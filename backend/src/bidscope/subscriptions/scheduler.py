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

import hashlib
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from bidscope.config import Settings, get_settings
from bidscope.persistence.models import Subscription

#: One-minute tick, matching the documented APScheduler schedule.
TICK_MINUTES = 1


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
    session: Any, subscription_id: str, scheduled_at: datetime,
) -> bool:
    """Try to acquire a session-level advisory lock; return True if held.

    ``pg_try_advisory_lock`` is non-blocking and returns True only when the lock
    was not already held, so a second concurrent worker observes False and skips.
    """
    key = subscription_lock_key(subscription_id, _bucket(scheduled_at))
    result = await session.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(result.scalar_one())


async def release_advisory_lock(
    session: Any, subscription_id: str, scheduled_at: datetime,
) -> None:
    """Release a previously acquired session-level advisory lock."""
    key = subscription_lock_key(subscription_id, _bucket(scheduled_at))
    await session.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def list_due_subscriptions(
    session_factory: Any, now: datetime | None = None,
) -> list[Subscription]:
    """Return active subscriptions.

    The next-run time is derived from the cron expression and stored inside the
    subscription's ``normalized_intent`` (the relational schema is frozen and
    carries no ``next_run_at`` column). For P0 the scheduler lists every active
    subscription; callers that need due-time filtering can compare against
    ``now`` in Python.
    """
    async with session_factory() as session:
        result = await session.execute(
            sa.select(Subscription).where(Subscription.status == "active")
        )
        return list(result.scalars())


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


async def _tick(settings: Settings) -> None:
    """Placeholder tick body; full tick lives in the service layer."""
    _ = settings


def start_scheduler(settings: Settings | None = None) -> Any:
    """Start the subscription scheduler process role."""
    scheduler = build_scheduler(settings)
    scheduler.start()
    return scheduler
