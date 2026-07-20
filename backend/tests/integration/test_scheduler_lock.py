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
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.db import create_engine_and_session
from bidscope.persistence.models import InboxEvent, QueryRun, Subscription
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter


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
) -> None:
    """Dual-worker lock: concurrent triggers for the same subscription/time
    bucket yield exactly one query run and one set of inbox events."""
    from bidscope.subscriptions.service import SubscriptionService

    _, session_factory = create_engine_and_session()
    service = SubscriptionService(session_factory=session_factory)

    sub_id = _next_id("sub-lock")
    sub = _make_subscription(subscription_id=sub_id)
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    # Fire two concurrent trigger attempts for the same subscription.
    await asyncio.gather(
        service.run_subscription(sub_id),
        service.run_subscription(sub_id),
        return_exceptions=True,
    )

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
