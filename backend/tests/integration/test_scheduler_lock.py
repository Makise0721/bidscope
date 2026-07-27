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
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.api.dependencies import RunService, build_demo_graph
from bidscope.clock import FixedClock
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore
from bidscope.graph.executor import _to_plain_dsn
from bidscope.persistence.models import InboxEvent, QueryRun, Subscription
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


def _next_id(name: str) -> str:
    """Generate a deterministic UUID for a named test subscription."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"

BATCH_1 = Path("data/demo/batch-1")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _test_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_mode="test",
        database_url=TEST_DB_URL,
        checkpoint_database_url=TEST_CHECKPOINT_URL,
        real_model_enabled=False,
        admin_token="test-admin-token",
        object_store_root=str(tmp_path / "objects"),
        test_control_token="test-controls-token",
    )


async def _reset_import_tracking(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
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
    sub = Subscription(
        id=subscription_id,
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
        normalized_intent={
            "regions": ["四川"],
            "topics": ["服务器"],
            "__source_run_id": "source-run-seed",
            "__user_request": "四川省服务器招标",
            "__next_run_at": "2026-07-20T09:00:00+00:00",
            "__consecutive_failures": 0,
        },
        status="active",
        trigger_key=f"trigger-{subscription_id}",
    )
    return sub


@pytest_asyncio.fixture()
async def real_run_service(
    imported_batch_1: None, tmp_path: Path,
) -> Any:
    """A real RunService + Postgres checkpointer for tests that exercise the
    live execution path of ``_run_locked``.

    The fixture imports batch-1 so the real graph retrieves notices and the
    scheduled subscription run produces a persisted report.
    """
    _, session_factory = create_engine_and_session()
    settings = _test_settings(tmp_path)
    object_store = LocalObjectStore(root=settings.object_store_root)
    dsn = _to_plain_dsn(settings.checkpoint_database_dsn())
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        graph = build_demo_graph(
            session_factory,
            settings,
            checkpointer=checkpointer,
            clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
            object_store=object_store,
        )
        service = RunService(
            session_factory, graph, object_store, settings,
            clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        )
        yield service


@pytest_asyncio.fixture()
async def imported_batch_1() -> None:
    await _import_bundle(BATCH_1)


@pytest.mark.asyncio
async def test_two_concurrent_triggers_produce_exactly_one_run(
    real_run_service: RunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual-worker lock: concurrent triggers for the same subscription/time
    bucket yield exactly one query run and one set of inbox events."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    session_factory = real_run_service.session_factory
    service = SubscriptionService(
        session_factory=session_factory, run_service=real_run_service,
    )

    sub_id = _next_id("sub-lock")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    barrier = asyncio.Barrier(2)
    loser_observed = asyncio.Event()
    original_acquire = service_module.acquire_advisory_lock

    async def synchronized_acquire(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        acquired = await original_acquire(connection, subscription_id, scheduled_at)
        if acquired:
            try:
                await asyncio.wait_for(loser_observed.wait(), timeout=5)
            except BaseException as wait_error:
                try:
                    await service_module.release_advisory_lock(  # type: ignore[attr-defined]
                        connection, subscription_id, scheduled_at,
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
async def test_successful_run_releases_lock_before_reusing_data_connection(
    real_run_service: RunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released run must not leave its advisory lock on a pooled connection."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.scheduler import subscription_lock_key
    from bidscope.subscriptions.service import SubscriptionService

    session_factory = real_run_service.session_factory
    engine = cast(AsyncEngine, session_factory.kw["bind"])
    service = SubscriptionService(
        session_factory=session_factory, run_service=real_run_service,
    )
    sub_id = _next_id("sub-release-connection")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    original_acquire = service_module.acquire_advisory_lock
    original_release = service_module.release_advisory_lock
    acquire_pids: list[int] = []
    release_pids: list[int] = []

    async def observing_acquire(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        acquire_pids.append(
            (
                await connection.execute(sa.text("SELECT pg_backend_pid()"))
            ).scalar_one()
        )
        return await original_acquire(connection, subscription_id, scheduled_at)

    async def observing_release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        release_pids.append(
            (
                await connection.execute(sa.text("SELECT pg_backend_pid()"))
            ).scalar_one()
        )
        return await original_release(connection, subscription_id, scheduled_at)

    monkeypatch.setattr(service_module, "acquire_advisory_lock", observing_acquire)
    monkeypatch.setattr(service_module, "release_advisory_lock", observing_release)
    try:
        first = await service.run_subscription(
            sub_id, scheduled_at=scheduled_at,
        )
        second = await service.run_subscription(
            sub_id, scheduled_at=scheduled_at,
        )

        assert first["skipped"] is False
        assert second["skipped"] is False
        assert len(acquire_pids) == 2
        assert release_pids == acquire_pids

        key = subscription_lock_key(
            sub_id, scheduled_at.replace(second=0, microsecond=0).isoformat(),
        )
        unsigned_key = key & ((1 << 64) - 1)
        async with session_factory() as observer:
            lock_count = (
                await observer.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = ANY(:pids) "
                        "AND classid = :classid AND objid = :objid "
                        "AND objsubid = 1"
                    ),
                    {
                        "pids": acquire_pids,
                        "classid": unsigned_key >> 32,
                        "objid": unsigned_key & 0xFFFFFFFF,
                    },
                )
            ).scalar_one()
        assert lock_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_during_final_release_unlocks_for_independent_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final-release cancellation drains the unlock before it propagates."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.scheduler import (
        acquire_advisory_lock,
        release_advisory_lock,
    )
    from bidscope.subscriptions.service import SubscriptionService

    engine, session_factory = create_engine_and_session()
    observer_engine, _observer_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)
    sub_id = str(uuid.uuid4())
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    release_finished = asyncio.Event()
    original_release = service_module.release_advisory_lock

    async def delayed_release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        release_started.set()
        await asyncio.wait_for(allow_release.wait(), timeout=5)
        released = await original_release(connection, subscription_id, scheduled_at)
        release_finished.set()
        return bool(released)

    async def immediate_run(
        session: AsyncSession,
        subscription: Subscription,
        scheduled_at: datetime,
    ) -> dict[str, object]:
        del session, subscription, scheduled_at
        return {
            "new_notices": 0,
            "material_changes": 0,
            "unchanged": 0,
            "failed": False,
            "skipped": False,
        }

    monkeypatch.setattr(service_module, "release_advisory_lock", delayed_release)
    monkeypatch.setattr(service, "_run_locked", immediate_run)
    task: asyncio.Task[dict[str, object]] | None = None
    try:
        async with session_factory() as session:
            session.add(_make_subscription(subscription_id=sub_id))
            await session.commit()

        task = asyncio.create_task(
            service.run_subscription(sub_id, scheduled_at=scheduled_at),
        )
        await asyncio.wait_for(release_started.wait(), timeout=5)
        task.cancel()
        allow_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        async with observer_engine.connect() as observer:
            observer_acquired = await acquire_advisory_lock(
                observer, sub_id, scheduled_at,
            )
            if observer_acquired:
                assert await release_advisory_lock(observer, sub_id, scheduled_at)
            await observer.commit()

        assert observer_acquired is True
        assert release_finished.is_set()
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await observer_engine.dispose()
        await engine.dispose()


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

    class _FakeLockConnection:
        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    async def delayed_acquire(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        del connection, subscription_id, scheduled_at
        acquisition_started.set()
        await asyncio.wait_for(finish_acquisition.wait(), timeout=5)
        acquisition_ready_to_return.set()
        await asyncio.wait_for(allow_acquisition_return.wait(), timeout=5)
        acquisition_completed.set()
        return True

    async def observing_release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        nonlocal release_calls
        del connection, subscription_id, scheduled_at
        release_calls += 1
        return True

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

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)
    monkeypatch.setattr(service, "_run_locked", unexpected_run_locked)

    task: asyncio.Task[dict[str, object]] | None = None
    try:
        task = asyncio.create_task(
            service.run_subscription(
                sub_id, scheduled_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
            )
        )
        await asyncio.wait_for(acquisition_started.wait(), timeout=5)
        task.cancel()
        finish_acquisition.set()
        await asyncio.wait_for(acquisition_ready_to_return.wait(), timeout=5)
        task.cancel()
        allow_acquisition_return.set()
        await asyncio.wait_for(acquisition_completed.wait(), timeout=5)

        with pytest.raises(asyncio.CancelledError):
            await task

        assert release_calls == 1
        assert run_locked_called is False
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_subscription_skips_work_when_cancelled_during_post_acquire_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation drained for the acquire commit prevents subscription work."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    sub_id = _next_id("sub-cancelled-post-acquire-commit")
    sub = _make_subscription(subscription_id=sub_id)
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
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

        async def commit(self) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeLockConnection:
        async def commit(self) -> None:
            commit_started.set()
            await asyncio.wait_for(allow_commit.wait(), timeout=5)

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    async def acquire(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        del connection, subscription_id, scheduled_at
        return True

    async def release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        nonlocal release_calls
        del connection, subscription_id, scheduled_at
        release_calls += 1
        return True

    async def unexpected_run_locked(
        session: AsyncSession, subscription: Subscription, scheduled_at: datetime,
    ) -> dict[str, object]:
        nonlocal run_locked_called
        del session, subscription, scheduled_at
        run_locked_called = True
        return {}

    monkeypatch.setattr(service_module, "acquire_advisory_lock", acquire)
    monkeypatch.setattr(service_module, "release_advisory_lock", release)
    service = SubscriptionService(_FakeSessionFactory())

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)
    monkeypatch.setattr(service, "_run_locked", unexpected_run_locked)

    task: asyncio.Task[dict[str, object]] | None = None
    try:
        task = asyncio.create_task(
            service.run_subscription(
                sub_id, scheduled_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
            )
        )
        await asyncio.wait_for(commit_started.wait(), timeout=5)
        task.cancel()
        allow_commit.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert run_locked_called is False
        assert release_calls == 1
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_subscription_preserves_operation_error_when_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-release failures are chained without replacing the run failure."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    class _RunFailure(Exception):
        pass

    class _ReleaseFailure(Exception):
        pass

    sub_id = _next_id("sub-run-error-release-failure")
    sub = _make_subscription(subscription_id=sub_id)
    run_error = _RunFailure("run failed")
    release_error = _ReleaseFailure("release failed")

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, subscription_id: str) -> Subscription:
            del model
            assert subscription_id == sub_id
            return sub

        async def commit(self) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeLockConnection:
        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    async def raise_run_error(
        session: AsyncSession, subscription: Subscription, scheduled_at: datetime,
    ) -> dict[str, object]:
        del session, subscription, scheduled_at
        raise run_error

    async def raise_release_error(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> None:
        del connection, subscription_id, scheduled_at
        raise release_error

    service = SubscriptionService(_FakeSessionFactory())

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(
        service_module, "acquire_advisory_lock", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)
    monkeypatch.setattr(service, "_run_locked", raise_run_error)
    monkeypatch.setattr(
        service_module, "_release_lock_connection_safely", raise_release_error,
    )

    with pytest.raises(_RunFailure) as caught:
        await service.run_subscription(
            sub_id, scheduled_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
        )

    assert caught.value is run_error
    assert caught.value.__cause__ is release_error


@pytest.mark.asyncio
async def test_run_subscription_normalizes_naive_scheduled_at(
    real_run_service: RunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A naive scheduled timestamp is normalized to UTC through the run."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import SubscriptionService

    session_factory = real_run_service.session_factory
    service = SubscriptionService(
        session_factory=session_factory, run_service=real_run_service,
    )

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
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        observed["acquire"] = scheduled_at
        return await original_acquire(connection, subscription_id, scheduled_at)

    async def observing_release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        observed["release"] = scheduled_at
        return await original_release(connection, subscription_id, scheduled_at)

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


@pytest.mark.asyncio
async def test_direct_run_ignores_stale_scheduled_occurrence_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual runs bypass the scheduler-only occurrence freshness guard."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import KEY_NEXT_RUN_AT, SubscriptionService

    sub_id = _next_id("sub-direct-stale-next-run")
    stale_next_run = datetime(2025, 7, 20, 9, tzinfo=UTC)
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    sub = _make_subscription(subscription_id=sub_id)
    sub.normalized_intent[KEY_NEXT_RUN_AT] = stale_next_run.isoformat()
    run_result: dict[str, object] = {
        "new_notices": 0,
        "material_changes": 0,
        "unchanged": 0,
        "failed": False,
        "skipped": False,
    }

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, subscription_id: str) -> Subscription:
            del model
            assert subscription_id == sub_id
            return sub

        async def commit(self) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return session

    class _FakeLockConnection:
        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    session = _FakeSession()
    acquire = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)
    guard = Mock(
        side_effect=AssertionError("direct runs must not check scheduler state"),
    )
    run_locked = AsyncMock(return_value=run_result)
    service = SubscriptionService(
        cast(async_sessionmaker[AsyncSession], _FakeSessionFactory()),
    )

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(service_module, "acquire_advisory_lock", acquire)
    monkeypatch.setattr(service_module, "release_advisory_lock", release)
    monkeypatch.setattr(service_module, "_matches_scheduled_occurrence", guard)
    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)
    monkeypatch.setattr(service, "_run_locked", run_locked)

    result = await service.run_subscription(
        sub_id, scheduled_at=scheduled_at, advance_schedule=False,
    )

    assert result is run_result
    guard.assert_not_called()
    run_locked.assert_awaited_once_with(session, sub, scheduled_at)
    assert sub.normalized_intent[KEY_NEXT_RUN_AT] == stale_next_run.isoformat()


@pytest.mark.asyncio
async def test_run_subscription_advances_schedule_before_releasing_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The successful schedule update is committed while the lock is held.

    Under the Task 4 execution path, ``_run_locked`` calls the injected
    ``run_service.create_run(run_key=<scheduled key>)`` and
    ``run_service.execute_run(...)`` (auto-approving any awaiting-confirmation
    interrupt), then gates on the persisted online report before committing the
    schedule update. This test pins those entry points.
    """
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import KEY_NEXT_RUN_AT, SubscriptionService

    sub_id = _next_id("sub-atomic-schedule")
    sub = _make_subscription(subscription_id=sub_id)
    sub.normalized_intent["__source_run_id"] = "source-run-id"
    sub.normalized_intent["__user_request"] = "subscription run"
    initial_next_run = "2026-07-20T09:00:00+00:00"
    sub.normalized_intent[KEY_NEXT_RUN_AT] = initial_next_run
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    next_run = datetime(2026, 7, 27, 9, tzinfo=UTC)
    events: list[object] = []
    lock_held = False

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, model: object, subscription_id: str) -> Subscription:
            del model
            assert subscription_id == sub_id
            return sub

        async def commit(self) -> None:
            events.append(("commit", lock_held, sub.normalized_intent[KEY_NEXT_RUN_AT]))

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeLockConnection:
        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    async def acquire(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        del connection, subscription_id, scheduled_at
        nonlocal lock_held
        events.append("acquire")
        lock_held = True
        return True

    async def release(
        connection: AsyncConnection,
        subscription_id: str,
        scheduled_at: datetime,
    ) -> bool:
        del connection, subscription_id, scheduled_at
        nonlocal lock_held
        events.append(("release", lock_held))
        lock_held = False
        return True

    class _RecordingRunService:
        def __init__(self) -> None:
            self.create_run_calls: list[tuple[str, dict[str, str] | None]] = []
            self.execute_run_calls: list[tuple[str, dict[str, str]]] = []
            self.confirm_calls: list[str] = []

        async def create_run(
            self, user_request: str, *, run_key: str | None = None,
        ) -> tuple[str, bool]:
            self.create_run_calls.append((user_request, {"run_key": run_key} if run_key else None))
            return "scheduled-run-id", True

        async def execute_run(
            self, run_id: str, input: dict[str, str], *, force_fresh: bool = False,
        ) -> dict[str, object]:
            self.execute_run_calls.append((run_id, dict(input)))
            return {"status": "completed"}

        async def confirm(self, run_id: str) -> dict[str, object]:
            self.confirm_calls.append(run_id)
            return {"status": "completed"}

    run_service = _RecordingRunService()

    monkeypatch.setattr(service_module, "acquire_advisory_lock", acquire)
    monkeypatch.setattr(service_module, "release_advisory_lock", release)
    monkeypatch.setattr(
        service_module, "_compute_next_run", Mock(return_value=next_run),
    )
    service = SubscriptionService(
        _FakeSessionFactory(), run_service=cast(Any, run_service),
    )

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)

    # The report gate must observe a persisted online report. Inject a stub
    # loader that returns a non-null report for the scheduled run id.
    async def _report_loader(run_id: str) -> object:
        del run_id
        return object()

    monkeypatch.setattr(service, "_load_persisted_report", _report_loader)
    monkeypatch.setattr(service, "_notice_views_from_report", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_diff_and_emit",
        AsyncMock(return_value={
            "new_notices": 0,
            "material_changes": 0,
            "unchanged": 0,
            "failed": False,
            "skipped": False,
        }),
    )
    monkeypatch.setattr(service, "_advance_seen", AsyncMock())

    result = await service.run_subscription(
        sub_id, scheduled_at=scheduled_at, advance_schedule=True,
    )

    assert result["failed"] is False
    assert len(run_service.create_run_calls) == 1
    user_request, kwargs = run_service.create_run_calls[0]
    assert user_request == "subscription run"
    # The scheduled run key is deterministic over subscription id + bucket.
    assert kwargs is not None
    expected_bucket = scheduled_at.replace(second=0, microsecond=0).isoformat()
    assert kwargs["run_key"] == f"subscription:{sub_id}:{expected_bucket}"
    assert run_service.execute_run_calls == [
        ("scheduled-run-id", {"user_request": "subscription run"}),
    ]
    assert sub.normalized_intent[KEY_NEXT_RUN_AT] == next_run.isoformat()
    assert events == [
        "acquire",
        ("commit", True, initial_next_run),
        ("commit", True, next_run.isoformat()),
        ("release", True),
    ]


@pytest.mark.asyncio
async def test_subscription_does_not_execute_existing_query_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idempotent subscription run must not execute an existing scheduled run.

    Under Task 4, ``_run_locked`` calls ``run_service.create_run`` first; when
    the scheduled run key already exists (``created=False``), the run has
    already been executed and ``execute_run`` must NOT be invoked again — the
    existing persisted report is the single source of truth.
    """
    from bidscope.subscriptions.service import SubscriptionService

    subscription = _make_subscription(subscription_id=_next_id("sub-existing-run"))
    subscription.normalized_intent["__source_run_id"] = "source-run-id"
    subscription.normalized_intent["__user_request"] = "subscription run"
    session = Mock()
    session.commit = AsyncMock()

    class _RecordingRunService:
        def __init__(self) -> None:
            self.execute_run_calls: list[tuple] = []

        async def create_run(
            self, user_request: str, *, run_key: str | None = None,
        ) -> tuple[str, bool]:
            return "existing-scheduled-run", False

        async def execute_run(
            self, run_id: str, input: object, *, force_fresh: bool = False,
        ) -> dict[str, object]:
            self.execute_run_calls.append((run_id, input))
            return {"status": "completed"}

        async def confirm(self, run_id: str) -> dict[str, object]:
            return {"status": "completed"}

    run_service = _RecordingRunService()
    service = SubscriptionService(
        cast(async_sessionmaker[AsyncSession], Mock()),
        run_service=cast(Any, run_service),
    )

    monkeypatch.setattr(service, "_load_persisted_report", AsyncMock(return_value=object()))
    monkeypatch.setattr(service, "_notice_views_from_report", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_diff_and_emit",
        AsyncMock(return_value={
            "new_notices": 0,
            "material_changes": 0,
            "unchanged": 0,
            "failed": False,
            "skipped": False,
        }),
    )
    monkeypatch.setattr(service, "_advance_seen", AsyncMock())

    result = await service._run_locked(
        session, subscription, datetime(2026, 7, 20, 9, tzinfo=UTC),
    )

    assert result["failed"] is False
    assert run_service.execute_run_calls == [], (
        "an existing scheduled run must not be re-executed"
    )


@pytest.mark.asyncio
async def test_run_subscription_failure_does_not_advance_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run retains its persisted next-run timestamp."""
    from bidscope.subscriptions import service as service_module
    from bidscope.subscriptions.service import KEY_NEXT_RUN_AT, SubscriptionService

    sub_id = _next_id("sub-failed-schedule")
    sub = _make_subscription(subscription_id=sub_id)
    initial_next_run = "2026-07-20T09:00:00+00:00"
    sub.normalized_intent[KEY_NEXT_RUN_AT] = initial_next_run
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    session = Mock()
    session.get = AsyncMock(return_value=sub)
    session.commit = AsyncMock()

    class _FakeSessionContext:
        async def __aenter__(self) -> Mock:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSessionContext:
            return _FakeSessionContext()

    class _FakeLockConnection:
        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def invalidate(self) -> None:
            return None

    monkeypatch.setattr(service_module, "acquire_advisory_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(service_module, "release_advisory_lock", AsyncMock(return_value=True))
    compute = Mock(side_effect=AssertionError("failed runs must not compute next run"))
    monkeypatch.setattr(service_module, "_compute_next_run", compute)
    service = SubscriptionService(_FakeSessionFactory(), fail_every_run=True)

    async def open_fake_lock_connection() -> AsyncConnection:
        return cast(AsyncConnection, _FakeLockConnection())

    monkeypatch.setattr(service, "_open_lock_connection", open_fake_lock_connection)

    result = await service.run_subscription(
        sub_id, scheduled_at=scheduled_at, advance_schedule=True,
    )

    assert result["failed"] is True
    assert sub.normalized_intent[KEY_NEXT_RUN_AT] == initial_next_run
    compute.assert_not_called()
    assert session.commit.await_count == 2


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


@pytest.mark.asyncio
async def test_serial_workers_skip_a_consumed_scheduled_occurrence(
    real_run_service: RunService,
) -> None:
    """A later worker must not replay an occurrence consumed by an earlier one."""
    from bidscope.subscriptions.service import KEY_NEXT_RUN_AT, SubscriptionService

    engine_a: AsyncEngine
    session_factory_a: async_sessionmaker[AsyncSession]
    engine_a, session_factory_a = create_engine_and_session()
    engine_b: AsyncEngine
    session_factory_b: async_sessionmaker[AsyncSession]
    engine_b, session_factory_b = create_engine_and_session()
    # Both services share the single real run service so a scheduled run key
    # created by A is observed by B as ``created=False`` (the relational row is
    # the cross-worker coordination boundary, alongside the advisory lock).
    service_a = SubscriptionService(
        session_factory=session_factory_a, run_service=real_run_service,
    )
    service_b = SubscriptionService(
        session_factory=session_factory_b, run_service=real_run_service,
    )
    sub_id = _next_id("sub-serial-scheduled-occurrence")
    scheduled_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    sub = _make_subscription(subscription_id=sub_id)
    sub.normalized_intent[KEY_NEXT_RUN_AT] = scheduled_at.isoformat()

    try:
        async with session_factory_a() as session:
            session.add(sub)
            await session.commit()

        first = await service_a.run_subscription(
            sub_id, scheduled_at=scheduled_at, advance_schedule=True,
        )

        assert first["failed"] is False
        assert first["skipped"] is False

        async with session_factory_b() as observer:
            persisted_sub = await observer.get(Subscription, sub_id)
            assert persisted_sub is not None
            next_run_after_a = persisted_sub.normalized_intent[KEY_NEXT_RUN_AT]
            assert next_run_after_a != scheduled_at.isoformat()
            run_count_after_a = (
                await observer.execute(
                    sa.select(sa.func.count()).select_from(QueryRun)
                )
            ).scalar_one()
            inbox_count_after_a = (
                await observer.execute(
                    sa.select(sa.func.count()).where(
                        InboxEvent.subscription_id == sub_id
                    )
                )
            ).scalar_one()

        second = await service_b.run_subscription(
            sub_id, scheduled_at=scheduled_at, advance_schedule=True,
        )

        assert second["failed"] is False

        async with session_factory_b() as observer:
            persisted_sub = await observer.get(Subscription, sub_id)
            assert persisted_sub is not None
            assert persisted_sub.normalized_intent[KEY_NEXT_RUN_AT] == next_run_after_a
            run_count_after_b = (
                await observer.execute(
                    sa.select(sa.func.count()).select_from(QueryRun)
                )
            ).scalar_one()
            inbox_count_after_b = (
                await observer.execute(
                    sa.select(sa.func.count()).where(
                        InboxEvent.subscription_id == sub_id
                    )
                )
            ).scalar_one()

        assert run_count_after_b == run_count_after_a
        assert inbox_count_after_b == inbox_count_after_a
        assert second["skipped"] is True
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
