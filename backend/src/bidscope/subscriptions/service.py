"""Subscription service: lifecycle, incremental runs, and inbox events.

A :class:`~bidscope.persistence.models.Subscription` is a confirmed, stored
schedule. :class:`SubscriptionService` owns the run lifecycle:

* :meth:`create_from_run` materializes an active subscription from a
  completed confirmed run and computes its next run time from the cron
  expression.
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
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.delivery.reports import PersistedReport, ReportPersistence
from bidscope.domain.intents import SearchIntent
from bidscope.persistence.models import (
    InboxEvent,
    NoticeEvidence,
    NoticeVersion,
    QueryRun,
    SourceNotice,
    Subscription,
    SubscriptionSeenItem,
)
from bidscope.retrieval.deduplication import (
    MaterialChange,
    NoticeView,
    detect_material_changes,
)
from bidscope.subscriptions.scheduler import (
    acquire_advisory_lock,
    release_advisory_lock,
)

#: Internal keys persisted inside ``normalized_intent`` (namespace-prefixed so
#: they never collide with operator-visible intent fields).
KEY_NEXT_RUN_AT = "__next_run_at"
KEY_CONSECUTIVE_FAILURES = "__consecutive_failures"
#: Run id (of a completed, confirmed run) whose ``search_intent`` seeded this
#: subscription; also the source of the ``__user_request`` replayed each tick.
KEY_SOURCE_RUN_ID = "__source_run_id"
KEY_USER_REQUEST = "__user_request"


async def _drain_task[T](task: asyncio.Future[T]) -> T:
    """Wait for a task to finish while preserving cancellation for the caller."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class RunServiceLike(Protocol):
    """The narrow slice of :class:`~bidscope.api.dependencies.RunService` the
    subscription bridge depends on.

    Declared as a Protocol so the API's :class:`RunService` satisfies it
    structurally without an import cycle (``api.dependencies`` imports the
    graph builder; this module stays free of that dependency).
    """

    session_factory: async_sessionmaker[AsyncSession]

    async def create_run(
        self, user_request: str, *, run_key: str | None = None,
    ) -> tuple[str, bool]: ...

    async def execute_run(
        self, run_id: str, input: Any, *, force_fresh: bool = False,
    ) -> dict[str, Any]: ...

    async def confirm(self, run_id: str) -> dict[str, Any]: ...


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


class SubscriptionCreateError(LookupError):
    """The source run for a subscription does not exist."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run not found: {run_id}")


class SubscriptionIntentError(ValueError):
    """The source run cannot seed a subscription (wrong status or no schedule)."""


def _scheduled_run_key(subscription_id: str, scheduled_at: datetime) -> str:
    """Deterministic, idempotent run key for one (subscription, minute) bucket.

    Two concurrent scheduler ticks for the same subscription + scheduled
    minute resolve to the same key, so :func:`create_run` deduplicates them
    into a single ``QueryRun`` row.
    """
    bucket = scheduled_at.replace(second=0, microsecond=0).isoformat()
    return f"subscription:{subscription_id}:{bucket}"


class SubscriptionService:
    """Run-lifecycle operations for incremental tender subscriptions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fail_every_run: bool = False,
        run_service: RunServiceLike | None = None,
        report_persistence: ReportPersistence | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.fail_every_run = fail_every_run
        # Injected collaborator that owns the real graph, checkpointer, and run
        # lifecycle. The scheduler process builds it itself; the API reuses the
        # one already wired into ``app.state``. ``None`` is permitted only for
        # legacy adapter tests that never exercise ``_run_locked``'s execution.
        self.run_service = run_service
        # Injected report gate. When omitted, the service lazily builds one over
        # a throw-away no-op object store (sufficient because
        # ``load_online_report`` only touches the relational session).
        self._report_persistence = report_persistence

    async def _open_lock_connection(self) -> AsyncConnection:
        """Open the dedicated pinned connection for one advisory-lock lifecycle."""
        return await _lock_engine(self.session_factory).connect()

    # ----------------------------------------------------------- lifecycle

    async def create_from_run(self, run_id: str) -> Subscription:
        """Materialize a confirmed subscription from a completed confirmed run.

        Contract:

        * The run must exist (404 otherwise, surfaced as :class:`LookupError`).
        * The run must be ``completed`` and have an intent carrying a non-empty
          ``schedule`` (409 otherwise, surfaced as :class:`ValueError`).
        * The persisted ``normalized_intent`` is the run's normalized
          :class:`SearchIntent` (re-validated from its stored JSON), plus
          internal keys recording the source run id, user request, and the
          computed next run time. ``cron_expression`` / ``timezone`` come from
          the intent's schedule and are never overridden by the API caller.
        """
        import uuid

        if self.run_service is None:
            raise RuntimeError(
                "create_from_run requires an injected run_service to confirm "
                "the source run, but none was supplied",
            )

        async with self.session_factory() as session:
            run = await session.get(QueryRun, run_id)
            if run is None:
                raise SubscriptionCreateError(run_id)
            if run.status != "completed":
                raise SubscriptionIntentError(
                    f"run is not completed (status={run.status!r})",
                )
            intent = self._resolve_intent(run)
            if intent.schedule is None:
                raise SubscriptionIntentError(
                    "run's search_intent carries no schedule; a subscription "
                    "requires a recurring schedule",
                )
            cron_expression = intent.schedule.cron_expression
            timezone = intent.schedule.timezone
            user_request = run.user_request
            next_run = _compute_next_run(cron_expression, timezone)
            stored_intent: dict[str, Any] = dict(
                intent.model_dump(mode="json"),
            )
            stored_intent[KEY_SOURCE_RUN_ID] = run_id
            stored_intent[KEY_USER_REQUEST] = user_request
            stored_intent[KEY_NEXT_RUN_AT] = next_run.isoformat()
            stored_intent[KEY_CONSECUTIVE_FAILURES] = 0
            subscription = Subscription(
                id=str(uuid.uuid4()),
                cron_expression=cron_expression,
                timezone=timezone,
                normalized_intent=stored_intent,
                status="active",
                trigger_key=str(uuid.uuid4()),
            )
            session.add(subscription)
            await record_audit_event(
                session,
                AuditContext(
                    method="POST",
                    path="/api/subscriptions",
                    run_id=run_id,
                    subscription_id=str(subscription.id),
                ),
                AuditEventType.SUBSCRIPTION_CREATED,
                AuditOutcome.SUCCESS,
                {
                    "status": subscription.status,
                    "cron_expression": subscription.cron_expression,
                },
            )
            await session.commit()
            await session.refresh(subscription)
        return subscription

    @staticmethod
    def _resolve_intent(run: QueryRun) -> SearchIntent:
        """Re-validate the run's stored ``search_intent`` into a model.

        Stored intents come from the graph's normalized ``SearchIntent`` (see
        ``RunService._update_status``), so the round-trip is lossless; this
        guards against partial writes and surfaces intent corruption loudly
        (as a 409-shaped :class:`SubscriptionIntentError`, not a 500-shaped
        pydantic ``ValidationError``).
        """
        from pydantic import ValidationError

        stored = run.search_intent or {}
        if not isinstance(stored, dict) or not stored:
            raise SubscriptionIntentError(
                "run's search_intent is empty; nothing to subscribe to",
            )
        try:
            return SearchIntent.model_validate(stored)
        except ValidationError as error:
            raise SubscriptionIntentError(
                f"run's search_intent is corrupted: {error}",
            ) from error

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
        """Execute one subscription occurrence while holding the advisory lock.

        Sequence:

        1. Create (or load) the scheduled ``QueryRun`` keyed by
           :func:`_scheduled_run_key` so concurrent workers contend on a single
           row, then drive the real graph via the injected run service and
           auto-resume any ``awaiting_confirmation`` interrupt.
        2. Gate on the persisted online report; a missing report is a failure.
        3. Diff the report's notice views against the seen set, emitting
           ``new_notice`` / ``material_change`` events, and advance the seen
           cursor only after the run commits.

        Crash-recovery contract for stuck scheduled runs: this method only
        drives *freshly created* runs (``created=True``). If a prior worker
        crashed after creating a ``pending``/``retryable``/``awaiting_confirmation``
        run, the next tick sees ``created=False`` and proceeds to the report
        gate, which finds no report and records a failure until the run is
        repaired. Stuck scheduled runs are recovered by the standard
        stale-run-recovery path (``mark_stale_runs_retryable`` at startup →
        ``RunService.retry`` for ``retryable`` / the API confirm path for
        ``awaiting_confirmation``), not by this tick. Dispatching to those
        state-specific recovery legs here was considered and deferred: it
        would duplicate the dedicated recovery machinery and risk changing
        external side effects (e.g. auto-confirming an ``awaiting_confirmation``
        run on every tick).
        """
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

        if self.run_service is None:
            raise RuntimeError(
                "_run_locked requires an injected run_service; the scheduler "
                "and API must construct SubscriptionService with one",
            )

        intent = sub.normalized_intent or {}
        user_request = intent.get(KEY_USER_REQUEST)
        if not isinstance(user_request, str) or not user_request:
            # Corrupt state: the subscription was never (or no longer is)
            # seeded from a confirmed run. Treat this as a failure so the
            # occurrence is retried after the operator repairs the data.
            await self._record_failure(session, sub)
            await session.commit()
            return {
                "new_notices": 0,
                "material_changes": 0,
                "unchanged": 0,
                "failed": True,
                "skipped": False,
            }

        # 1. Idempotent scheduled run + real graph execution.
        run_id, created = await self.run_service.create_run(
            user_request, run_key=_scheduled_run_key(sub.id, scheduled_at),
        )
        if created:
            first = await self.run_service.execute_run(
                run_id, {"user_request": user_request},
            )
            # A scheduled query always routes through ``confirm_intent``; the
            # subscription bridge auto-approves the interrupt so the run
            # proceeds to retrieval and delivery.
            if first.get("status") == "awaiting_confirmation":
                first = await self.run_service.confirm(run_id)
            status = first.get("status")
            if status not in ("completed",):
                # ``retryable`` / ``failed`` are failures: do not advance the
                # cursor or count this as a success.
                await self._record_failure(session, sub)
                await session.commit()
                return {
                    "new_notices": 0,
                    "material_changes": 0,
                    "unchanged": 0,
                    "failed": True,
                    "skipped": False,
                }

        # 2. Report gate: the persisted online report is the durable proof the
        #    run actually delivered. A missing report is a failure and never
        #    advances the seen cursor.
        persisted = await self._load_persisted_report(run_id)
        if persisted is None:
            await self._record_failure(session, sub)
            await session.commit()
            return {
                "new_notices": 0,
                "material_changes": 0,
                "unchanged": 0,
                "failed": True,
                "skipped": False,
            }

        # 3. Build the notice views the report references and diff.
        notices = await self._notice_views_from_report(session, persisted)

        stats = await self._diff_and_emit(session, sub, notices)

        # 4. Advance the seen-item cursor only now (after the report commits).
        await self._advance_seen(session, sub, notices)

        # 5. Reset the failure counter on success.
        sub.last_successful_run_at = scheduled_at
        new_intent = dict(sub.normalized_intent or {})
        new_intent[KEY_CONSECUTIVE_FAILURES] = 0
        if advance_schedule:
            new_intent[KEY_NEXT_RUN_AT] = _compute_next_run(
                sub.cron_expression,
                sub.timezone,
                after=scheduled_at,
            ).isoformat()
        sub.normalized_intent = new_intent
        await session.commit()
        return stats

    async def _load_persisted_report(self, run_id: str) -> PersistedReport | None:
        """Load the durable online report for ``run_id`` via the report bridge.

        Exposed as an overridable method so adapter tests can stub the gate
        without reaching into :class:`ReportPersistence` directly.
        """
        persistence = self._report_persistence
        if persistence is None:
            persistence = ReportPersistence(self.session_factory, _NoObjectStore())
        return await persistence.load_online_report(run_id)

    async def _notice_views_from_report(
        self,
        session: AsyncSession,
        persisted: PersistedReport,
    ) -> list[_NoticesMatch]:
        """Materialize the latest :class:`NoticeView` per notice the report cites.

        Each report item references a notice version id; the diff must compare
        the *latest* version of that source notice (so a re-imported batch is
        reflected), not the version the original report cited. Evidence texts
        are attached to support the material-change comparator.
        """
        notice_version_ids = [
            item.notice_id for item in persisted.report.items if item.notice_id
        ]
        if not notice_version_ids:
            return []

        # Map each cited version to its source notice so we can resolve the
        # latest version per source notice.
        cited_rows = (
            await session.execute(
                sa.select(NoticeVersion.id, NoticeVersion.source_notice_id).where(
                    NoticeVersion.id.in_(notice_version_ids)
                )
            )
        ).all()
        source_notice_ids = {str(row.source_notice_id) for row in cited_rows}
        if not source_notice_ids:
            return []

        latest_versions = await self._latest_versions(session, source_notice_ids)
        views: list[_NoticesMatch] = []
        for source_notice_id, version in latest_versions.items():
            source = await session.get(SourceNotice, source_notice_id)
            if source is None:
                continue
            evidence_texts = tuple(
                (
                    await session.execute(
                        sa.select(NoticeEvidence.text)
                        .where(NoticeEvidence.notice_version_id == version.id)
                        .order_by(NoticeEvidence.id)
                    )
                ).scalars()
            )
            views.append(_NoticesMatch(
                source_id=str(source.id),
                view=self._build_notice_view(source, version, evidence_texts),
            ))
        return views

    @staticmethod
    def _build_notice_view(
        source: SourceNotice,
        version: NoticeVersion,
        evidence_texts: tuple[str, ...],
    ) -> NoticeView:
        """Construct the comparison view for one notice + its latest version."""
        return NoticeView(
            source=source.source,
            external_id=source.external_id,
            canonical_url=source.source_url,
            project_number=source.project_number,
            content_hash=version.content_hash,
            title=version.title,
            purchaser=version.purchaser,
            region=version.region,
            budget_minor_units=version.budget_minor_units,
            budget_currency=version.budget_currency,
            deadline=version.deadline,
            claim_supporting_texts=evidence_texts,
        )

    @staticmethod
    async def _latest_versions(
        session: AsyncSession, source_notice_ids: set[str],
    ) -> dict[str, NoticeVersion]:
        """Return the latest :class:`NoticeVersion` per source notice id."""
        latest = (
            sa.select(
                NoticeVersion.source_notice_id,
                sa.func.max(NoticeVersion.created_at).label("max_created"),
            )
            .where(NoticeVersion.source_notice_id.in_(source_notice_ids))
            .group_by(NoticeVersion.source_notice_id)
            .subquery()
        )
        statement = (
            sa.select(NoticeVersion)
            .join(
                latest,
                sa.and_(
                    latest.c.source_notice_id == NoticeVersion.source_notice_id,
                    latest.c.max_created == NoticeVersion.created_at,
                ),
            )
        )
        rows = (await session.execute(statement)).scalars()
        return {str(version.source_notice_id): version for version in rows}

    async def _diff_and_emit(
        self,
        session: AsyncSession,
        sub: Subscription,
        notices: list[_NoticesMatch],
    ) -> dict[str, Any]:
        """Diff notice views against the seen set and emit inbox events.

        A notice whose content hash differs from its previously seen version
        is re-checked via :func:`detect_material_changes`: only material
        fields (deadline/budget/region/purchaser/scope/cancellation/claims)
        emit a ``material_change`` event. A formatting-only hash change counts
        as ``unchanged`` and advances the cursor without an event.
        """
        # Load the previously seen views so material-change comparison has a
        # full ``NoticeView`` for the prior version, not just the hash.
        previous_views = await self._load_seen_views(session, sub.id)
        seen_hashes: dict[str, str] = {
            notice_id: view.content_hash
            for notice_id, view in previous_views.items()
        }

        new_notices = 0
        material_changes = 0
        unchanged = 0
        for match in notices:
            previous = previous_views.get(match.source_id)
            previous_hash = seen_hashes.get(match.source_id)
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
                if previous is not None:
                    changes = detect_material_changes(previous, match.view)
                else:
                    changes = [MaterialChange(
                        field="content_hash",
                        before=previous_hash,
                        after=match.view.content_hash,
                    )]
                if changes:
                    material_changes += 1
                    session.add(InboxEvent(
                        subscription_id=sub.id,
                        event_type="material_change",
                        notice_id=match.source_id,
                        title=match.view.title,
                        message=f"Material change in: {match.view.title}",
                    ))
                else:
                    # Formatting-only content-hash change: no event, treated as
                    # unchanged for stats but the cursor still advances below.
                    unchanged += 1
            else:
                unchanged += 1
        return {
            "new_notices": new_notices,
            "material_changes": material_changes,
            "unchanged": unchanged,
            "failed": False,
            "skipped": False,
        }

    async def _load_seen_views(
        self, session: AsyncSession, subscription_id: str,
    ) -> dict[str, NoticeView]:
        """Reconstruct the previously seen :class:`NoticeView`s for the diff.

        ``SubscriptionSeenItem`` stores the content hash at the time of the
        last successful run, not the full view. For the material-change
        comparator we need the previously observed material fields; rather
        than persist them (which would require a schema change), we re-load
        the source notice / version row whose hash matches the seen item.
        If the matching version has been deleted (e.g. by a fresh import),
        we fall back to comparing only the hash so the cursor still advances.
        """
        items = (
            await session.execute(
                sa.select(SubscriptionSeenItem).where(
                    SubscriptionSeenItem.subscription_id == subscription_id
                )
            )
        ).scalars()
        views: dict[str, NoticeView] = {}
        for item in items:
            source = await session.get(SourceNotice, item.notice_id)
            if source is None:
                continue
            version = (
                await session.execute(
                    sa.select(NoticeVersion).where(
                        NoticeVersion.source_notice_id == item.notice_id,
                        NoticeVersion.content_hash == item.version_content_hash,
                    ).order_by(NoticeVersion.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if version is None:
                continue
            evidence_texts = tuple(
                (
                    await session.execute(
                        sa.select(NoticeEvidence.text)
                        .where(NoticeEvidence.notice_version_id == version.id)
                        .order_by(NoticeEvidence.id)
                    )
                ).scalars()
            )
            views[str(item.notice_id)] = self._build_notice_view(
                source, version, evidence_texts,
            )
        return views

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


class _NoObjectStore:
    """Throw-away :class:`~bidscope.delivery.objects.ObjectStore` stand-in.

    Used only to satisfy :class:`ReportPersistence`'s constructor when no real
    store has been injected. ``load_online_report`` never touches the store, so
    every method raises if it is accidentally reached.
    """

    def put_bytes(self, key: str, data: bytes) -> str:
        raise RuntimeError("subscription report gate must not write objects")

    def get_bytes(self, key: str) -> bytes:
        raise RuntimeError("subscription report gate must not read objects")

    def exists(self, key: str) -> bool:
        return False

    def list_keys(self, prefix: str = "") -> list[str]:
        raise RuntimeError("subscription report gate must not list objects")

    def delete(self, key: str) -> None:
        raise RuntimeError("subscription report gate must not delete objects")
