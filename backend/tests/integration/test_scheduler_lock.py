"""Integration tests for the PostgreSQL advisory-lock scheduler.

The subscription scheduler uses a PostgreSQL advisory lock derived from the
subscription UUID and the scheduled time bucket. Two concurrent trigger
attempts for the same subscription/time bucket must result in exactly one query
run and one set of inbox events — the second worker must observe that the lock
is already held and skip.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.db import create_engine_and_session
from bidscope.persistence.models import InboxEvent, QueryRun, Subscription
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter
from sqlalchemy.ext.asyncio import AsyncSession


def _next_id(name: str) -> str:
    """Generate a deterministic UUID for a named test subscription."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"

BATCH_1 = Path("data/demo/batch-1")


async def _reset_import_tracking(session_factory: object) -> None:
    async with session_factory() as session:
        await session.execute(sa.text(
            "TRUNCATE TABLE snapshot_imports, snapshot_bundles, "
            "subscriptions, subscription_seen_items, inbox_events CASCADE"
        ))
        await session.commit()


async def _import_bundle(bundle: Path) -> None:
    _, session_factory = create_engine_and_session()
    await _reset_import_tracking(session_factory)
    importer = SnapshotImporter(
        session_factory=session_factory,
        repository_factory=SnapshotRepository,
    )
    await importer.import_bundle(bundle)


def _make_subscription(*, subscription_id: str) -> Subscription:
    return Subscription(
        id=subscription_id,
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
        normalized_intent={"regions": ["四川"], "topics": ["服务器"]},
        status="active",
        trigger_key=f"trigger-{subscription_id}",
    )


@pytest_asyncio.fixture()
async def imported_batch_1() -> None:
    await _import_bundle(BATCH_1)


@pytest.mark.asyncio
async def test_two_concurrent_triggers_produce_exactly_one_run(
    imported_batch_1: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual-worker lock: concurrent triggers for the same subscription/time
    bucket yield exactly one query run and one set of inbox events."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-lock")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    barrier = asyncio.Barrier(2)
    loser_observed = asyncio.Event()
    original_acquire = service_module.acquire_advisory_lock

    async def synchronized_acquire(
        session: object, subscription_id: str, scheduled_at: datetime,
    ) -> bool:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        acquired = await original_acquire(session, subscription_id, scheduled_at)
        if acquired:
            try:
                await asyncio.wait_for(loser_observed.wait(), timeout=5)
            except BaseException as wait_error:
                try:
                    await service_module.release_advisory_lock(  # type: ignore[attr-defined]
                        session, subscription_id, scheduled_at,
                    )
                except BaseException as release_error:
                    raise wait_error from release_error
                raise
        else:
            loser_observed.set()
        return acquired

    monkeypatch.setattr(
        service_module, "acquire_advisory_lock", synchronized_acquire,
    )

    # A shared timestamp makes both attempts use the same advisory-lock bucket.
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    tasks = [
        asyncio.create_task(
            service.run_subscription(sub_id, scheduled_at=scheduled_at),
        ),
        asyncio.create_task(
            service.run_subscription(sub_id, scheduled_at=scheduled_at),
        ),
    ]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        raise
    finally:
        unfinished = [task for task in tasks if not task.done()]
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    assert len(results) == 2
    assert all(isinstance(result, dict) for result in results)
    assert all(result["failed"] is False for result in results)
    assert sum(result["skipped"] is True for result in results) == 1

    # Exactly one query run was created (the second concurrent worker must
    # observe the held advisory lock and skip).
    async with session_factory() as session:
        run_count = (
            await session.execute(sa.select(sa.func.count()).select_from(QueryRun))
        ).scalar_one()
        inbox_count = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    InboxEvent.subscription_id == sub_id
                )
            )
        ).scalar_one()

    assert run_count == 1, f"expected exactly one run, got {run_count}"
    # The single run produced one event per newly seen notice.
    assert inbox_count > 0


@pytest.mark.asyncio
async def test_run_subscription_releases_lock_when_cancelled_repeatedly_during_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation during acquisition still releases a later lock."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    sub_id = _next_id("sub-cancelled-acquire")
    sub = _make_subscription(subscription_id=sub_id)
    acquisition_started = asyncio.Event()
    finish_acquisition = asyncio.Event()
    acquisition_ready_to_return = asyncio.Event()
    allow_acquisition_return = asyncio.Event()
    acquisition_completed = asyncio.Event()
    release_calls = 0
    run_locked_called = False

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, subscription_id: str) -> Subscription:
            del model
            assert subscription_id == sub_id
            return sub

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    async def delayed_acquire(
        session: object, subscription_id: str, scheduled_at: datetime,
    ) -> bool:
        del session, subscription_id, scheduled_at
        acquisition_started.set()
        await finish_acquisition.wait()
        acquisition_ready_to_return.set()
        await allow_acquisition_return.wait()
        acquisition_completed.set()
        return True

    async def observing_release(
        session: object, subscription_id: str, scheduled_at: datetime,
    ) -> None:
        nonlocal release_calls
        del session, subscription_id, scheduled_at
        release_calls += 1

    async def unexpected_run_locked(
        session: AsyncSession, subscription: Subscription, scheduled_at: datetime,
    ) -> dict[str, object]:
        nonlocal run_locked_called
        del session, subscription, scheduled_at
        run_locked_called = True
        return {}

    monkeypatch.setattr(
        service_module, "acquire_advisory_lock", delayed_acquire,
    )
    monkeypatch.setattr(
        service_module, "release_advisory_lock", observing_release,
    )
    service = SubscriptionService(_FakeSessionFactory())
    monkeypatch.setattr(service, "_run_locked", unexpected_run_locked)

    task = asyncio.create_task(
        service.run_subscription(
            sub_id, scheduled_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
        )
    )
    await acquisition_started.wait()
    task.cancel()
    finish_acquisition.set()
    await acquisition_ready_to_return.wait()
    task.cancel()
    allow_acquisition_return.set()
    await acquisition_completed.wait()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert release_calls == 1
    assert run_locked_called is False


@pytest.mark.asyncio
async def test_run_subscription_normalizes_naive_scheduled_at(
    imported_batch_1: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A naive scheduled timestamp is normalized to UTC through the run."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-naive-scheduled-at")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    naive_scheduled_at = datetime(2026, 7, 20, 9)
    normalized_scheduled_at = naive_scheduled_at.replace(tzinfo=UTC)
    observed: dict[str, datetime] = {}

    original_acquire = service_module.acquire_advisory_lock
    original_release = service_module.release_advisory_lock
    original_run_locked = service._run_locked

    async def observing_acquire(
        session: object, subscription_id: str, scheduled_at: datetime,
    ) -> bool:
        observed["acquire"] = scheduled_at
        return await original_acquire(session, subscription_id, scheduled_at)

    async def observing_release(
        session: object, subscription_id: str, scheduled_at: datetime,
    ) -> None:
        observed["release"] = scheduled_at
        await original_release(session, subscription_id, scheduled_at)

    async def observing_run_locked(
        session: AsyncSession, subscription: Subscription, scheduled_at: datetime,
    ) -> dict[str, object]:
        observed["run_locked"] = scheduled_at
        return await original_run_locked(session, subscription, scheduled_at)

    monkeypatch.setattr(
        service_module, "acquire_advisory_lock", observing_acquire,
    )
    monkeypatch.setattr(
        service_module, "release_advisory_lock", observing_release,
    )
    monkeypatch.setattr(service, "_run_locked", observing_run_locked)

    result = await service.run_subscription(
        sub_id, scheduled_at=naive_scheduled_at,
    )

    assert result["failed"] is False
    assert observed["acquire"] == normalized_scheduled_at
    assert observed["run_locked"] == normalized_scheduled_at
    assert observed["release"] == normalized_scheduled_at

    async with session_factory() as session:
        persisted_sub = await session.get(Subscription, sub_id)
    assert persisted_sub is not None
    assert persisted_sub.last_successful_run_at == normalized_scheduled_at


def test_compute_next_run_normalizes_naive_reference_to_utc() -> None:
    """Naive references passed to APScheduler are interpreted as UTC."""
    from bidscope.subscriptions import service

    naive_reference = datetime(2026, 7, 20, 8, 30)
    captured: dict[str, datetime] = {}

    class _Trigger:
        @classmethod
        def from_crontab(cls, expression: str, timezone: object) -> _Trigger:
            del expression, timezone
            return cls()

        def get_next_fire_time(
            self, previous_fire_time: datetime, now: datetime,
        ) -> datetime:
            captured["previous"] = previous_fire_time
            captured["now"] = now
            return datetime(2026, 7, 20, 9, tzinfo=UTC)

    original_trigger = service.CronTrigger
    service.CronTrigger = _Trigger  # type: ignore[assignment]
    try:
        service._compute_next_run("0 9 * * *", "Asia/Shanghai", naive_reference)
    finally:
        service.CronTrigger = original_trigger

    assert captured["previous"] == naive_reference.replace(tzinfo=UTC)
    assert captured["now"] == naive_reference.replace(tzinfo=UTC)


def test_compute_next_run_treats_numeric_one_as_monday() -> None:
    """Project crontab expressions use 1 for Monday."""
    from bidscope.subscriptions.service import _compute_next_run

    reference = datetime(2026, 7, 19, 8, 30, tzinfo=UTC)

    next_run = _compute_next_run("0 9 * * 1", "UTC", reference)

    assert next_run == datetime(2026, 7, 20, 9, tzinfo=UTC)


@pytest.mark.parametrize("sunday_value", ["0", "7"])
def test_compute_next_run_treats_numeric_sunday_values_as_sunday(
    sunday_value: str,
) -> None:
    """Both standard crontab aliases for Sunday map to APScheduler's Sunday."""
    from bidscope.subscriptions.service import _compute_next_run

    reference = datetime(2026, 7, 18, 8, 30, tzinfo=UTC)

    next_run = _compute_next_run(f"0 9 * * {sunday_value}", "UTC", reference)

    assert next_run == datetime(2026, 7, 19, 9, tzinfo=UTC)


def test_normalize_crontab_day_of_week_expands_named_steps() -> None:
    """Named weekday ranges with steps use standard crontab numbering."""
    from bidscope.subscriptions.service import _normalize_crontab_day_of_week

    normalized = _normalize_crontab_day_of_week("0 9 * * MON-FRI/2")

    assert normalized == "0 9 * * mon,wed,fri"


@pytest.mark.parametrize(
    "day_of_week",
    [
        pytest.param("*/8", id="wildcard-step"),
        pytest.param("*/999", id="oversized-wildcard-step"),
        pytest.param("1-5/6", id="numeric-range-step"),
        pytest.param("MON-FRI/999", id="named-range-step"),
        pytest.param("MON/8", id="named-step"),
    ],
)
def test_normalize_crontab_day_of_week_rejects_oversized_steps(
    day_of_week: str,
) -> None:
    """Unsupported DOW steps must not be silently truncated by range()."""
    from bidscope.subscriptions.service import _normalize_crontab_day_of_week

    with pytest.raises(ValueError):
        _normalize_crontab_day_of_week(f"0 9 * * {day_of_week}")


@pytest.mark.parametrize(
    "day_of_week",
    ["MON2", "MON-", "MON-1", "SUN-7", "1-8/2", "*/0", "mon-fri/x"],
)
def test_normalize_crontab_day_of_week_rejects_malformed_tokens(
    day_of_week: str,
) -> None:
    """Malformed or out-of-range DOW tokens must fail before APScheduler."""
    from bidscope.subscriptions.service import _normalize_crontab_day_of_week

    with pytest.raises(ValueError):
        _normalize_crontab_day_of_week(f"0 9 * * {day_of_week}")


@pytest.mark.asyncio
async def test_advisory_lock_key_derives_from_subscription_and_time() -> None:
    """The advisory lock key is deterministically derived from the subscription
    UUID and the scheduled time bucket."""
    from bidscope.subscriptions.scheduler import subscription_lock_key

    key1 = subscription_lock_key("sub-abc", "2026-07-20T09:00:00+08:00")
    key2 = subscription_lock_key("sub-abc", "2026-07-20T09:00:00+08:00")
    key3 = subscription_lock_key("sub-abc", "2026-07-20T10:00:00+08:00")
    key4 = subscription_lock_key("sub-xyz", "2026-07-20T09:00:00+08:00")

    assert key1 == key2, "same subscription + time must yield the same key"
    assert key1 != key3, "different time buckets must yield different keys"
    assert key1 != key4, "different subscriptions must yield different keys"
    assert isinstance(key1, int), "advisory lock keys must be integers"
