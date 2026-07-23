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

import asyncio
import dataclasses
import re
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

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


async def _drain_task[T](task: asyncio.Future[T]) -> T:
    """Wait for a task to finish while preserving cancellation for the caller."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _await_cleanup_safely[T](coroutine: Awaitable[T]) -> T:
    """Finish a cleanup operation even when its caller is cancelled."""
    cleanup: asyncio.Future[T] = asyncio.ensure_future(coroutine)
    return await _drain_task(cleanup)


async def _acquire_advisory_lock_safely(
    connection: AsyncConnection,
    subscription_id: str,
    scheduled_at: datetime,
) -> bool:
    """Acquire a lock without losing ownership if the caller is cancelled."""
    acquisition = asyncio.create_task(
        acquire_advisory_lock(connection, subscription_id, scheduled_at),
    )
    try:
        return await asyncio.shield(acquisition)
    except asyncio.CancelledError as cancellation_error:
        try:
            acquired = await _drain_task(acquisition)
        except BaseException as acquisition_error:
            raise cancellation_error from acquisition_error
        if acquired:
            try:
                await _release_lock_connection_safely(
                    connection, subscription_id, scheduled_at,
                )
            except BaseException as release_error:
                raise cancellation_error from release_error
        raise


def _lock_engine(session_factory: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    """Return the async engine bound to the service's session factory."""
    bind = session_factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("subscription session factory must be bound to an AsyncEngine")
    return bind


def _cancellation_count() -> int:
    """Return cancellation requests already active on this task."""
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


async def _release_lock_connection_safely(
    connection: AsyncConnection,
    subscription_id: str,
    scheduled_at: datetime,
) -> None:
    """Release, finalize, and retire a pinned advisory-lock connection safely."""
    cancellation_count = _cancellation_count()
    error: BaseException | None = None
    try:
        released = await _await_cleanup_safely(
            release_advisory_lock(connection, subscription_id, scheduled_at),
        )
        if not released:
            raise RuntimeError("PostgreSQL advisory lock was not held at release")
        await _await_cleanup_safely(connection.commit())
        await _await_cleanup_safely(connection.close())
    except BaseException as caught:
        errors = [caught]
        # A failed or unconfirmed unlock must never return the backend session to
        # the pool, because it may still own a session-level advisory lock.
        for cleanup in (connection.invalidate(), connection.close()):
            try:
                await _await_cleanup_safely(cleanup)
            except BaseException as cleanup_error:
                errors.append(cleanup_error)
        if len(errors) == 1:
            error = caught
        else:
            error = BaseExceptionGroup(
                "advisory-lock release and connection cleanup failed", errors,
            )

    if _cancellation_count() > cancellation_count:
        cancellation_error = asyncio.CancelledError()
        if error is not None:
            raise cancellation_error from error
        raise cancellation_error
    if error is not None:
        raise error


def _skipped_outcome() -> dict[str, Any]:
    """Return the standard nonfailure outcome for a skipped occurrence."""
    return {
        "new_notices": 0,
        "material_changes": 0,
        "unchanged": 0,
        "failed": False,
        "skipped": True,
    }


def _normalize_scheduled_at(value: datetime) -> datetime:
    """Normalize a requested occurrence timestamp to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _matches_scheduled_occurrence(
    subscription: Subscription,
    scheduled_at: datetime,
) -> bool:
    """Whether durable subscription state still names this exact occurrence."""
    if subscription.status != "active":
        return False
    intent = subscription.normalized_intent
    if not isinstance(intent, dict):
        return False
    stored = intent.get(KEY_NEXT_RUN_AT)
    if not isinstance(stored, str):
        return False
    try:
        return _normalize_scheduled_at(datetime.fromisoformat(stored)) == scheduled_at
    except ValueError:
        return False


# Project crontab expressions follow the conventional Sunday=0/7, Monday=1
# numbering, while APScheduler's numeric weekdays are Monday=0 through Sunday=6.
_STANDARD_CRON_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NAMED_CRON_WEEKDAYS = {
    name: value for value, name in enumerate(_STANDARD_CRON_WEEKDAYS)
}
_DOW_VALUE = r"(?:\d+|[A-Za-z]+)"
_DOW_TOKEN = re.compile(
    rf"(?P<start>{_DOW_VALUE})(?:-(?P<end>{_DOW_VALUE}))?"
    r"(?:/(?P<step>\d+))?$"
)
_DOW_ALL_TOKEN = re.compile(r"\*(?:/(?P<step>\d+))?$")


def _normalize_crontab_day_of_week(cron_expression: str) -> str:
    """Validate and expand standard crontab DOW syntax for APScheduler.

    Project expressions use Sunday=0/7 and Monday=1 through Saturday=6.
    APScheduler's numeric weekday syntax has different semantics, so every
    supported DOW token is expanded to lowercase weekday names. This also
    makes named ranges with steps explicit and prevents APScheduler's permissive
    prefix matching from silently accepting malformed tokens.
    """
    fields = cron_expression.split()
    if len(fields) != 5:
        return cron_expression

    day_of_week = fields[4]

    def weekday_name(value: int) -> str:
        if value == 7:
            value = 0
        if not 0 <= value <= 6:
            raise ValueError(f"invalid crontab day-of-week value: {value}")
        return _STANDARD_CRON_WEEKDAYS[value]

    def endpoint_value(raw: str) -> int:
        if raw.isdigit():
            value = int(raw)
            if not 0 <= value <= 7:
                raise ValueError(f"invalid crontab day-of-week value: {value}")
            return value
        named_value = _NAMED_CRON_WEEKDAYS.get(raw.lower())
        if named_value is None:
            raise ValueError(f"invalid crontab day-of-week name: {raw}")
        return named_value

    def expand_token(token: str) -> list[str]:
        all_match = _DOW_ALL_TOKEN.fullmatch(token)
        if all_match:
            step_raw = all_match.group("step")
            if step_raw is None:
                return ["*"]
            step = int(step_raw)
            if step <= 0:
                raise ValueError("crontab day-of-week step must be positive")
            if step > 7:
                raise ValueError(
                    "crontab day-of-week step exceeds its seven-day range"
                )
            return [weekday_name(value) for value in range(0, 7, step)]

        token_match = _DOW_TOKEN.fullmatch(token)
        if token_match is None:
            raise ValueError(f"invalid crontab day-of-week token: {token}")

        start_raw = token_match.group("start")
        start = endpoint_value(start_raw)
        end_raw = token_match.group("end")
        step_raw = token_match.group("step")
        if end_raw is not None and start_raw.isdigit() != end_raw.isdigit():
            raise ValueError(
                "crontab day-of-week range endpoints must use the same syntax"
            )
        end = endpoint_value(end_raw) if end_raw is not None else (
            7 if step_raw is not None else start
        )
        step = int(step_raw) if step_raw is not None else 1
        if step <= 0:
            raise ValueError("crontab day-of-week step must be positive")
        if step > 7:
            raise ValueError(
                "crontab day-of-week step exceeds its seven-day range"
            )
        if start > end:
            raise ValueError(
                "crontab day-of-week range start must not exceed its end"
            )
        if step > end - start + 1:
            raise ValueError(
                "crontab day-of-week step exceeds its range"
            )
        return [
            weekday_name(value)
            for value in range(start, end + 1, step)
        ]

    normalized_tokens: list[str] = []
    seen_weekdays: set[str] = set()
    for token in day_of_week.split(","):
        for name in expand_token(token):
            if name == "*":
                normalized_tokens.append(name)
            elif name not in seen_weekdays:
                seen_weekdays.add(name)
                normalized_tokens.append(name)

    fields[4] = ",".join(normalized_tokens)
    return " ".join(fields)


def _compute_next_run(
    cron_expression: str, timezone: str, after: datetime | None = None,
) -> datetime:
    """Compute the next run strictly after ``after`` using a five-field cron."""
    reference = after or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    normalized_expression = _normalize_crontab_day_of_week(cron_expression)
    trigger = CronTrigger.from_crontab(
        normalized_expression, timezone=ZoneInfo(timezone),
    )
    next_run = trigger.get_next_fire_time(reference, reference)
    if next_run is None:
        raise ValueError(f"cron expression has no next run: {cron_expression}")
    return cast(datetime, next_run)


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

    async def _open_lock_connection(self) -> AsyncConnection:
        """Open the dedicated pinned connection for one advisory-lock lifecycle."""
        return await _lock_engine(self.session_factory).connect()

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

    async def run_subscription(
        self,
        subscription_id: str,
        *,
        scheduled_at: datetime | None = None,
        advance_schedule: bool = False,
    ) -> dict[str, Any]:
        """Run one subscription cycle: lock, retrieve, diff, emit, advance.

        ``scheduled_at`` optionally supplies the scheduled timestamp used for
        advisory-lock bucketing; when omitted, the current UTC time is used.
        ``advance_schedule`` atomically advances the persisted next-run time on
        successful runs while the advisory lock remains held; it defaults to
        false for direct/manual callers.
        Returns a stats dict with keys ``new_notices``, ``material_changes``,
        ``unchanged``, ``failed``, and ``skipped``. ``skipped`` is true when
        another worker holds the lock or a scheduler occurrence was consumed.
        """
        if scheduled_at is None:
            scheduled_at = datetime.now(UTC)
        elif scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        else:
            scheduled_at = scheduled_at.astimezone(UTC)

        lock_connection = await self._open_lock_connection()
        acquired = False
        cancellation_count = _cancellation_count()
        primary_error: BaseException | None = None
        try:
            # Advisory locks are session-level. Keep this connection explicitly
            # pinned, but finish the acquire statement's transaction before
            # retrieval and graph work begin.
            acquired = await _acquire_advisory_lock_safely(
                lock_connection, subscription_id, scheduled_at,
            )
            if not acquired:
                await _await_cleanup_safely(lock_connection.commit())
                return _skipped_outcome()
            await _await_cleanup_safely(lock_connection.commit())
            if _cancellation_count() > cancellation_count:
                raise asyncio.CancelledError()

            async with self.session_factory() as session:
                sub = await session.get(Subscription, subscription_id)
                if sub is None:
                    if advance_schedule:
                        return _skipped_outcome()
                    raise KeyError(f"subscription not found: {subscription_id}")
                if advance_schedule and not _matches_scheduled_occurrence(
                    sub, scheduled_at,
                ):
                    return _skipped_outcome()
                # Finish the state-read transaction before graph execution. The
                # pinned lock connection retains advisory-lock ownership.
                await session.commit()
                if advance_schedule:
                    return await self._run_locked(
                        session, sub, scheduled_at, advance_schedule=True,
                    )
                return await self._run_locked(session, sub, scheduled_at)
        except BaseException as primary_caught:
            primary_error = primary_caught
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                if acquired:
                    await _release_lock_connection_safely(
                        lock_connection, subscription_id, scheduled_at,
                    )
                else:
                    await _await_cleanup_safely(lock_connection.close())
            except BaseException as cleanup_caught:
                cleanup_error = cleanup_caught

            if primary_error is not None:
                if cleanup_error is not None:
                    raise primary_error from cleanup_error
            elif _cancellation_count() > cancellation_count:
                cancellation_error = asyncio.CancelledError()
                if cleanup_error is not None:
                    raise cancellation_error from cleanup_error
                raise cancellation_error
            elif cleanup_error is not None:
                raise cleanup_error

    async def _run_locked(
        self,
        session: AsyncSession,
        sub: Subscription,
        scheduled_at: datetime,
        *,
        advance_schedule: bool = False,
    ) -> dict[str, Any]:
        """Execute the run while holding the advisory lock."""
        if self.fail_every_run:
            await self._record_failure(session, sub)
            await session.commit()
            return {
                "new_notices": 0,
                "material_changes": 0,
                "unchanged": 0,
                "failed": True,
                "skipped": False,
            }

        # 1. Create a query run (reuses the executor's run lifecycle so a
        #    ``QueryRun`` row exists for the dual-worker assertion).
        run_id, created = await create_run(
            f"subscription {sub.id}", session_factory=self.session_factory,
        )
        if created:
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
        if advance_schedule:
            intent[KEY_NEXT_RUN_AT] = _compute_next_run(
                sub.cron_expression,
                sub.timezone,
                after=scheduled_at,
            ).isoformat()
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
            "skipped": False,
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
