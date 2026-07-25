"""Subscription scheduler: APScheduler process role + PostgreSQL advisory locks.

Subscriptions are confirmed, stored schedules. The scheduler role ticks on a
one-minute cadence, and for each due subscription acquires a PostgreSQL
 advisory lock derived from the subscription UUID and the scheduled time bucket.
The lock guarantees that two concurrent workers acting on the same
subscription/time bucket execute the run exactly once.

The advisory lock key is a single signed 64-bit integer derived deterministically
from the subscription id and the bucketed scheduled time, so any two processes
that agree on those two values contend on the same lock.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from bidscope.config import Settings, get_settings
from bidscope.db import create_engine_and_session
from bidscope.persistence.models import Subscription

#: One-minute tick, matching the documented APScheduler schedule.
TICK_MINUTES = 1
KEY_NEXT_RUN_AT = "__next_run_at"


def _as_utc(value: datetime) -> datetime:
    """Normalize naive or offset-aware datetimes for due comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stored_next_run(subscription: Subscription) -> datetime | None:
    """Parse the persisted next-run timestamp, ignoring malformed state."""
    intent = subscription.normalized_intent
    if not isinstance(intent, dict):
        return None
    raw = intent.get(KEY_NEXT_RUN_AT)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


async def advance_subscription_next_run(
    subscription_id: str,
    *,
    session_factory: Any,
    now: datetime,
) -> None:
    """Persist the next cron occurrence after a successful scheduler outcome."""
    from bidscope.subscriptions.service import _compute_next_run

    async with session_factory() as session:
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None or subscription.status != "active":
            return
        next_run = _compute_next_run(
            subscription.cron_expression,
            subscription.timezone,
            after=now,
        )
        intent = dict(subscription.normalized_intent or {})
        intent[KEY_NEXT_RUN_AT] = next_run.isoformat()
        subscription.normalized_intent = intent
        await session.commit()


def subscription_lock_key(subscription_id: str, scheduled_time: str) -> int:
    """Derive a stable signed 64-bit advisory lock key from a subscription + time bucket.

    The key is the first 8 bytes of a SHA-256 over the subscription id and the
    bucketed scheduled time, reinterpreted as a signed 64-bit integer so it fits
    PostgreSQL's two-key ``bigint`` advisory lock API.
    """
    digest = hashlib.sha256(f"{subscription_id}::{scheduled_time}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _bucket(scheduled_at: datetime) -> str:
    """Floor a datetime to the minute for stable time-bucket locking."""
    return scheduled_at.replace(second=0, microsecond=0).isoformat()


async def acquire_advisory_lock(
    connection: AsyncConnection,
    subscription_id: str,
    scheduled_at: datetime,
) -> bool:
    """Try to acquire a session-level advisory lock; return True if held.

    ``pg_try_advisory_lock`` is non-blocking and returns True only when the lock
    was not already held, so a second concurrent worker observes False and skips.
    """
    key = subscription_lock_key(subscription_id, _bucket(scheduled_at))
    result = await connection.execute(
        sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key},
    )
    return bool(result.scalar_one())


async def release_advisory_lock(
    connection: AsyncConnection,
    subscription_id: str,
    scheduled_at: datetime,
) -> bool:
    """Release a previously acquired session-level advisory lock."""
    key = subscription_lock_key(subscription_id, _bucket(scheduled_at))
    result = await connection.execute(
        sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key},
    )
    return bool(result.scalar_one())


async def list_due_subscriptions(
    session_factory: Any, now: datetime | None = None,
) -> list[Subscription]:
    """Return active subscriptions whose persisted next run is due."""
    reference = _as_utc(now or datetime.now(UTC))
    async with session_factory() as session:
        result = await session.execute(
            sa.select(Subscription).where(Subscription.status == "active")
        )
        return [
            subscription
            for subscription in result.scalars()
            if subscription.status == "active"
            and (next_run := _stored_next_run(subscription)) is not None
            and next_run <= reference
        ]


def build_scheduler(settings: Settings | None = None) -> Any:
    """Build an APScheduler ``BackgroundScheduler`` for the subscription tick."""
    from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
        BackgroundScheduler,
    )
    from apscheduler.triggers.interval import (  # type: ignore[import-untyped]
        IntervalTrigger,
    )

    resolved = settings or get_settings()
    scheduler = BackgroundScheduler(timezone=getattr(resolved, "app_tz", "Asia/Shanghai"))
    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(minutes=TICK_MINUTES),
        id="subscription_tick",
        replace_existing=True,
        args=[resolved],
    )
    return scheduler


def _build_subscription_service(
    session_factory: Any,
    run_service: Any,
) -> Any:
    """Build the subscription service with a real run service + report gate.

    The scheduler runs in its own process and cannot share ``app.state``; the
    caller (``run_scheduler_tick``) builds the process-local :class:`RunService`
    and the matching :class:`ReportPersistence` gate and passes them here.
    """
    from bidscope.delivery.reports import ReportPersistence
    from bidscope.subscriptions.service import SubscriptionService

    report_persistence = ReportPersistence(
        session_factory, run_service.object_store,
    )
    return SubscriptionService(
        session_factory=session_factory,
        run_service=run_service,
        report_persistence=report_persistence,
    )


def _to_sync_dsn(async_url: str) -> str:
    """Convert an ``asyncpg`` DSN to a synchronous ``psycopg`` (v3) DSN."""
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


async def _run_due_subscriptions(
    session_factory: Any,
    due: list[Subscription],
    run_service: Any,
) -> dict[str, int]:
    """Drive each due subscription once, returning the tick counters.

    Extracted from :func:`run_scheduler_tick` so the per-subscription loop and
    its error accounting stay unit-testable independently of the process-local
    graph/checkpointer assembly. ``run_service`` is the process-local
    :class:`~bidscope.api.dependencies.RunService`; the matching
    :class:`~bidscope.subscriptions.service.SubscriptionService` is built from
    it on each tick.
    """
    service = _build_subscription_service(session_factory, run_service)
    counters = {"due": len(due), "ran": 0, "skipped": 0, "failed": 0}
    for subscription in due:
        scheduled_at = _stored_next_run(subscription)
        if scheduled_at is None:
            counters["failed"] += 1
            continue
        try:
            outcome = await service.run_subscription(
                subscription.id,
                scheduled_at=scheduled_at,
                advance_schedule=True,
            )
        except Exception:
            counters["failed"] += 1
            continue

        if outcome.get("failed"):
            counters["failed"] += 1
            continue
        if outcome.get("skipped"):
            # The lock owner advances this occurrence atomically in its run.
            # A skipped worker must not update the schedule.
            counters["skipped"] += 1
            continue
        counters["ran"] += 1
    return counters


async def run_scheduler_tick(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run every due subscription once and return tick counters.

    The scheduler is a separate process from the API, so it builds its own
    durable :class:`~langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`,
    compiles the real demo graph, and assembles a process-local
    :class:`~bidscope.api.dependencies.RunService` for the subscription bridge.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from sqlalchemy import orm

    from bidscope.api.dependencies import _build_run_service_components
    from bidscope.clock import SystemClock
    from bidscope.delivery.objects import LocalObjectStore
    from bidscope.graph.executor import _to_plain_dsn

    resolved = settings or get_settings()
    reference = _as_utc(now or datetime.now(UTC))
    engine, session_factory = create_engine_and_session(resolved)
    try:
        due = await list_due_subscriptions(session_factory, reference)
        if not due:
            return {"due": 0, "ran": 0, "skipped": 0, "failed": 0}

        # Process-local graph + run service over a dedicated checkpointer.
        sync_engine = sa.create_engine(_to_sync_dsn(resolved.database_url))
        sync_session_factory = orm.sessionmaker(bind=sync_engine)
        object_store = LocalObjectStore(root=resolved.object_store_root)
        dsn = _to_plain_dsn(resolved.checkpoint_database_url)
        try:
            async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
                run_service = _build_run_service_components(
                    resolved,
                    session_factory,
                    sync_session_factory,
                    object_store,
                    SystemClock(),
                    checkpointer,
                )
                return await _run_due_subscriptions(session_factory, due, run_service)
        finally:
            sync_engine.dispose()
    finally:
        await engine.dispose()


def _tick(settings: Settings) -> None:
    """Run one async scheduler tick from APScheduler's sync worker."""
    asyncio.run(run_scheduler_tick(settings))


def start_scheduler(settings: Settings | None = None) -> Any:
    """Start the subscription scheduler process role."""
    scheduler = build_scheduler(settings)
    scheduler.start()
    return scheduler
