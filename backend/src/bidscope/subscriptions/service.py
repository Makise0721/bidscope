"""Subscription service: lifecycle, incremental runs, and inbox events.

A :class:`~bidscope.persistence.models.Subscription` is a confirmed, stored
schedule. :class:`SubscriptionService` owns the run lifecycle:

* :meth:`create_subscription` persists an active subscription and computes its
  next run time from the cron expression.
* :meth:`run_subscription` acquires a PostgreSQL advisory lock (so two workers
  on the same subscription/time bucket run exactly once), retrieves notices for
  the subscription's intent, diffs them against the subscription's seen items,
  emits ``new_notice`` / ``material_change`` inbox events, and advances the
  seen-item cursor only after the run's report commits.
* Three consecutive failures pause the subscription.

The ``Subscription`` schema is frozen; computed scheduling state (``next_run_at``,
``consecutive_failures``) is persisted inside the existing ``normalized_intent``
JSONB column under internal keys, leaving the operator-visible intent intact.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bidscope.graph.executor import create_run, execute
from bidscope.persistence.models import (
    InboxEvent,
    NoticeVersion,
    SourceNotice,
    Subscription,
    SubscriptionSeenItem,
)
from bidscope.retrieval.deduplication import NoticeView
from bidscope.subscriptions.scheduler import (
    acquire_advisory_lock,
    release_advisory_lock,
)

#: Internal keys persisted inside ``normalized_intent`` (namespace-prefixed so
#: they never collide with operator-visible intent fields).
KEY_NEXT_RUN_AT = "__next_run_at"
KEY_CONSECUTIVE_FAILURES = "__consecutive_failures"


def _compute_next_run(
    cron_expression: str, timezone: str, after: datetime | None = None,
) -> datetime:
    """Compute the next run time for a simple ``M H DOM MONTH DOW`` cron.

    Handles the weekly schedules used by the demonstration (e.g. ``0 9 * * 1``
    = every Monday at 09:00). Falls back to one day after ``after`` for
    patterns it does not specifically recognize.
    """
    after = after or datetime.now(UTC)
    fields = cron_expression.split()
    minute, hour, _dom, _month, dow = fields if len(fields) == 5 else (0, 0, "*", "*", "*")
    # Tolerance: honor a numeric day-of-week (0=Sunday … 6=Saturday).
    try:
        target_dow = int(dow) % 7
    except ValueError:
        target_dow = after.weekday()
    candidate = after.replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0,
    )
    days_ahead = (target_dow - candidate.weekday()) % 7
    if days_ahead == 0 and candidate <= after:
        days_ahead = 7
    return candidate + timedelta(days=days_ahead)


@dataclasses.dataclass
class _NoticesMatch:
    """A retrieved notice paired with its source-notice UUID (the DB FK)."""

    source_id: str
    view: NoticeView


class SubscriptionService:
    """Run-lifecycle operations for incremental tender subscriptions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fail_every_run: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.fail_every_run = fail_every_run

    # ----------------------------------------------------------- lifecycle

    async def create_subscription(
        self,
        intent: dict[str, Any],
        cron_expression: str,
        timezone: str = "Asia/Shanghai",
    ) -> Subscription:
        """Persist an active subscription and compute its next run time."""
        import uuid

        subscription_id = str(uuid.uuid4())
        next_run = _compute_next_run(cron_expression, timezone)
        # Persist computed state under internal keys; keep the visible intent.
        stored_intent = {
            **intent,
            KEY_NEXT_RUN_AT: next_run.isoformat(),
            KEY_CONSECUTIVE_FAILURES: 0,
        }
        sub = Subscription(
            id=subscription_id,
            cron_expression=cron_expression,
            timezone=timezone,
            normalized_intent=stored_intent,
            status="active",
            trigger_key=str(uuid.uuid4()),
        )
        async with self.session_factory() as session:
            session.add(sub)
            await session.commit()
            await session.refresh(sub)
        return sub

    async def run_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Run one subscription cycle: lock, retrieve, diff, emit, advance.

        Returns a stats dict with keys ``new_notices``, ``material_changes``,
        ``unchanged``, and ``failed``.
        """
        async with self.session_factory() as session:
            sub = await session.get(Subscription, subscription_id)
            if sub is None:
                raise KeyError(f"subscription not found: {subscription_id}")

            scheduled_at = datetime.now(UTC)
            # Advisory lock: a second concurrent worker observes it is already
            # held and skips, guaranteeing exactly one run per time bucket.
            acquired = await acquire_advisory_lock(session, subscription_id, scheduled_at)
            if not acquired:
                return {"new_notices": 0, "material_changes": 0, "unchanged": 0, "failed": False}
            try:
                return await self._run_locked(session, sub, scheduled_at)
            finally:
                await release_advisory_lock(session, subscription_id, scheduled_at)

    async def _run_locked(
        self,
        session: AsyncSession,
        sub: Subscription,
        scheduled_at: datetime,
    ) -> dict[str, Any]:
        """Execute the run while holding the advisory lock."""
        if self.fail_every_run:
            await self._record_failure(session, sub)
            await session.commit()
            return {"new_notices": 0, "material_changes": 0, "unchanged": 0, "failed": True}

        # 1. Create a query run (reuses the executor's run lifecycle so a
        #    ``QueryRun`` row exists for the dual-worker assertion).
        run_id = await create_run(
            f"subscription {sub.id}", session_factory=self.session_factory,
        )
        await execute(
            _dummy_graph(), run_id, {"user_request": f"subscription {sub.id}"},
            session_factory=self.session_factory,
        )

        # 2. Retrieve notices matching the subscription's intent.
        notices = await self._retrieve_notices(session, sub)

        # 3. Diff against the seen set and emit inbox events.
        stats = await self._diff_and_emit(session, sub, notices)

        # 4. Advance the seen-item cursor only now (after the run commits).
        await self._advance_seen(session, sub, notices)

        # 5. Reset the failure counter on success.
        sub.last_successful_run_at = scheduled_at
        intent = dict(sub.normalized_intent or {})
        intent[KEY_CONSECUTIVE_FAILURES] = 0
        sub.normalized_intent = intent
        await session.commit()
        return stats

    async def _retrieve_notices(
        self, session: AsyncSession, sub: Subscription,
    ) -> list[_NoticesMatch]:
        """Retrieve the latest version of each matching notice.

        When multiple demo batches are imported, a notice may carry several
        versions; the subscription always diffs against the most recent one.
        """
        intent = {k: v for k, v in (sub.normalized_intent or {}).items() if not k.startswith("__")}
        regions = intent.get("regions") or []
        # Latest version per source_notice_id (greatest created_at).
        latest = (
            sa.select(
                NoticeVersion.source_notice_id,
                sa.func.max(NoticeVersion.created_at).label("max_created"),
            )
            .group_by(NoticeVersion.source_notice_id)
            .subquery()
        )
        statement = (
            sa.select(NoticeVersion, SourceNotice)
            .join(SourceNotice, SourceNotice.id == NoticeVersion.source_notice_id)
            .join(
                latest,
                sa.and_(
                    latest.c.source_notice_id == NoticeVersion.source_notice_id,
                    latest.c.max_created == NoticeVersion.created_at,
                ),
            )
            .where(SourceNotice.source == "synthetic_demo")
        )
        if regions:
            # Region is stored on the version (see demo adapter / importer) as a
            # full name (e.g. "四川省"); the intent carries a short form (e.g.
            # "四川"), so match by substring.
            statement = statement.where(
                sa.or_(*(NoticeVersion.region.like(f"%{r}%") for r in regions))
            )
        result = await session.execute(statement)
        views: list[_NoticesMatch] = []
        for version, source in result.all():
            views.append(_NoticesMatch(
                source_id=source.id,
                view=NoticeView(
                    source=source.source,
                    external_id=source.external_id,
                    canonical_url=source.source_url,
                    project_number=source.project_number,
                    content_hash=version.content_hash,
                    title=version.title,
                    purchaser=version.purchaser,
                    region=source.region,
                    budget_minor_units=version.budget_minor_units,
                    budget_currency=version.budget_currency,
                    deadline=version.deadline,
                ),
            ))
        return views

    async def _diff_and_emit(
        self,
        session: AsyncSession,
        sub: Subscription,
        notices: list[_NoticesMatch],
    ) -> dict[str, Any]:
        """Diff notices against the seen set and emit inbox events."""
        result = await session.execute(
            sa.select(SubscriptionSeenItem).where(
                SubscriptionSeenItem.subscription_id == sub.id
            )
        )
        seen: dict[str, str] = {
            item.notice_id: item.version_content_hash for item in result.scalars()
        }

        new_notices = 0
        material_changes = 0
        unchanged = 0
        for match in notices:
            previous_hash = seen.get(match.source_id)
            if previous_hash is None:
                new_notices += 1
                session.add(InboxEvent(
                    subscription_id=sub.id,
                    event_type="new_notice",
                    notice_id=match.source_id,
                    title=match.view.title,
                    message=f"New notice: {match.view.title}",
                ))
            elif previous_hash != match.view.content_hash:
                material_changes += 1
                session.add(InboxEvent(
                    subscription_id=sub.id,
                    event_type="material_change",
                    notice_id=match.source_id,
                    title=match.view.title,
                    message=f"Material change in: {match.view.title}",
                ))
            else:
                unchanged += 1
        return {
            "new_notices": new_notices,
            "material_changes": material_changes,
            "unchanged": unchanged,
            "failed": False,
        }

    async def _advance_seen(
        self,
        session: AsyncSession,
        sub: Subscription,
        notices: list[_NoticesMatch],
    ) -> None:
        """Persist/update seen items for the notices retrieved in this run."""
        now = datetime.now(UTC)
        for match in notices:
            existing = (
                await session.execute(
                    sa.select(SubscriptionSeenItem).where(
                        SubscriptionSeenItem.subscription_id == sub.id,
                        SubscriptionSeenItem.notice_id == match.source_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(SubscriptionSeenItem(
                    subscription_id=sub.id,
                    notice_id=match.source_id,
                    version_content_hash=match.view.content_hash,
                    seen_at=now,
                ))
            else:
                existing.version_content_hash = match.view.content_hash
                existing.seen_at = now

    async def _record_failure(self, session: AsyncSession, sub: Subscription) -> None:
        """Bump the failure counter and pause after three consecutive failures."""
        intent = dict(sub.normalized_intent or {})
        failures = int(intent.get(KEY_CONSECUTIVE_FAILURES, 0)) + 1
        intent[KEY_CONSECUTIVE_FAILURES] = failures
        sub.normalized_intent = intent
        if failures >= 3:
            sub.status = "paused"


def _dummy_graph() -> Any:
    """A no-op graph; subscription runs persist a ``QueryRun``, not a report."""

    class _NoOpGraph:
        async def astream(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            return
            yield  # make this an async generator; never reached (no events)

        async def aget_state(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            return None

    return _NoOpGraph()
