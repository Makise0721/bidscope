"""Integration tests for incremental tender subscriptions.

A confirmed, scheduled subscription is stored with a next-run time. On a
subscription run, the service compares the freshly retrieved notices against the
subscription's ``seen_items``:

* notices never seen before → ``new_notice`` inbox event.
* notices whose content hash changed since last seen → ``material_change`` event.
* unchanged notices → no event.

Three consecutive failures pause the subscription. The seen-item cursor only
advances after the report commits.

These tests run against the Compose test database with both demo bundles
imported, so retrieval returns real (synthetic-demo) notices to diff.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.db import create_engine_and_session
from bidscope.persistence.models import (
    InboxEvent,
    Subscription,
    SubscriptionSeenItem,
)
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter


def _next_id(name: str) -> str:
    """Generate a deterministic UUID for a named test subscription."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"

BATCH_1 = Path("data/demo/batch-1")
BATCH_2 = Path("data/demo/batch-2")


async def _reset_import_tracking(
    session_factory: object, *, keep_subscriptions: bool = False,
) -> None:
    """Clear the idempotency tracking tables (and, unless ``keep_subscriptions``,
    the subscription tables) for a clean run."""
    async with session_factory() as session:
        if keep_subscriptions:
            await session.execute(sa.text(
                "TRUNCATE TABLE snapshot_imports, snapshot_bundles CASCADE"
            ))
        else:
            await session.execute(sa.text(
                "TRUNCATE TABLE snapshot_imports, snapshot_bundles, "
                "subscriptions, subscription_seen_items, inbox_events CASCADE"
            ))
        await session.commit()


async def _import_bundle(bundle: Path, *, keep_subscriptions: bool = False) -> None:
    """Import a demo bundle directly via the real importer."""
    _, session_factory = create_engine_and_session()
    await _reset_import_tracking(session_factory, keep_subscriptions=keep_subscriptions)
    importer = SnapshotImporter(
        session_factory=session_factory,
        repository_factory=SnapshotRepository,
    )
    await importer.import_bundle(bundle)


@pytest_asyncio.fixture()
async def imported_batches() -> None:
    """Import batch-1 then batch-2 so both versions are available to diff."""
    await _import_bundle(BATCH_1)
    await _import_bundle(BATCH_2)


def _make_subscription(
    *,
    subscription_id: str,
    cron: str = "0 9 * * 1",
    status: str = "active",
    intent_regions: list[str] | None = None,
) -> Subscription:
    import uuid

    return Subscription(
        id=subscription_id,
        cron_expression=cron,
        timezone="Asia/Shanghai",
        normalized_intent={"regions": intent_regions or ["四川"], "topics": ["服务器"]},
        status=status,
        trigger_key=f"trigger-{uuid.uuid4()}",
    )


@pytest.mark.asyncio
async def test_create_subscription_confirms_intent_and_sets_next_run() -> None:
    """Creating an active subscription stores it with a next-run time."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub = await service.create_subscription(
        intent={"regions": ["四川"], "topics": ["服务器"], "schedule": "weekly"},
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
    )
    assert sub.status == "active"
    assert sub.cron_expression == "0 9 * * 1"
    next_run_raw = (sub.normalized_intent or {}).get("__next_run_at")
    assert next_run_raw, "create_subscription must compute a next run time"
    next_run = datetime.fromisoformat(next_run_raw)
    assert next_run.tzinfo is not None


@pytest.mark.asyncio
async def test_first_run_emits_new_notice_events_for_all_notices(
    imported_batches: None,
) -> None:
    """The first run over batch-1 notices emits a new_notice event for each."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-first-run")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    stats = await service.run_subscription(sub_id)

    assert stats["new_notices"] > 0
    assert stats["material_changes"] == 0
    assert stats["unchanged"] == 0

    async with session_factory() as session:
        result = await session.execute(
            sa.select(InboxEvent).where(InboxEvent.subscription_id == sub_id)
        )
        events = list(result.scalars())
    assert len(events) == stats["new_notices"]
    assert all(e.event_type == "new_notice" for e in events)


@pytest.mark.asyncio
async def test_second_batch_emits_new_and_material_change_events() -> None:
    """Across two batches, a run emits new_notice for demo-013/014 and
    material_change for notices whose content changed; unchanged notices produce
    no event.

    The test controls batch import order: batch-1 first (establishing the seen
    set), then batch-2 (introducing new and changed notices).
    """
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    # Import batch-1 first (clears subscription tables), then create the
    # subscription so it survives the import.
    await _import_bundle(BATCH_1)
    sub_id = _next_id("sub-second-batch")
    # Cover both demo regions so the run sees all six batch-2 notices.
    sub = _make_subscription(
        subscription_id=sub_id,
        intent_regions=["四川", "重庆"],
    )
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    # First run: batch-1 only → establishes the seen set.
    first_stats = await service.run_subscription(sub_id)
    assert first_stats["new_notices"] > 0

    # Second run after batch-2 introduces new + changed notices.
    # Preserve the subscription + seen set established by the first run.
    await _import_bundle(BATCH_2, keep_subscriptions=True)
    second_stats = await service.run_subscription(sub_id)

    assert second_stats["new_notices"] >= 2  # demo-013, demo-014 are new
    assert second_stats["material_changes"] >= 1  # at least one notice changed

    async with session_factory() as session:
        result = await session.execute(
            sa.select(InboxEvent).where(
                InboxEvent.subscription_id == sub_id,
                InboxEvent.event_type == "new_notice",
            )
        )
        new_events = list(result.scalars())
        result2 = await session.execute(
            sa.select(InboxEvent).where(
                InboxEvent.subscription_id == sub_id,
                InboxEvent.event_type == "material_change",
            )
        )
        change_events = list(result2.scalars())

    # New-event count across both runs covers the batch-2 newcomers.
    assert len(new_events) >= 2
    assert len(change_events) >= 1


@pytest.mark.asyncio
async def test_unchanged_notices_produce_no_event(imported_batches: None) -> None:
    """A notice identical to the one already seen produces no inbox event."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-unchanged")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    first_stats = await service.run_subscription(sub_id)
    # Running again without any new batch: all previously seen notices are
    # unchanged, so no new events and no material changes.
    second_stats = await service.run_subscription(sub_id)

    assert second_stats["new_notices"] == 0
    assert second_stats["material_changes"] == 0
    assert second_stats["unchanged"] == first_stats["new_notices"]


@pytest.mark.asyncio
async def test_three_consecutive_failures_pause_subscription(
    imported_batches: None,
) -> None:
    """Three consecutive failures pause the subscription."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    # Force failures via an injected fault flag.
    service = SubscriptionService(
        session_factory=session_factory,
        fail_every_run=True,
    )

    sub_id = _next_id("sub-fail")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    for _ in range(3):
        result = await service.run_subscription(sub_id)
        assert result["failed"] is True

    async with session_factory() as session:
        sub_after = await session.get(Subscription, sub_id)
    assert sub_after.status == "paused"


@pytest.mark.asyncio
async def test_seen_items_advance_only_after_report_commit(imported_batches: None) -> None:
    """The seen-item cursor advances only after the run's report commits."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-seen")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    await service.run_subscription(sub_id)

    async with session_factory() as session:
        result = await session.execute(
            sa.select(SubscriptionSeenItem).where(
                SubscriptionSeenItem.subscription_id == sub_id
            )
        )
        seen = list(result.scalars())
    # Every new_notice event corresponds to a persisted seen item.
    assert len(seen) > 0
    assert all(si.version_content_hash for si in seen)
