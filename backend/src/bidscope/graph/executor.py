"""Durable execution and cross-instance recovery for the query workflow.

Two pieces sit on top of the compiled graph (:func:`~bidscope.graph.builder.build_graph`):

* :func:`execute` — drives a graph from an input (a fresh ``user_request`` or a
  :class:`~langgraph.types.Command` resume) while streaming its progress and
  persisting each node event to the ``run_events`` table.
* :func:`setup_checkpoints`` — creates the LangGraph checkpoint tables. This is
  the ONLY place that calls the checkpointer's ``setup()`` and it is invoked
  exclusively from the ``bidscope checkpoints setup`` CLI command; the executor
  never calls it implicitly.

Graphs are bound to an
:class:`~langgraph.checkpoint.postgres.aio.AsyncPostgresSaver` built from
``settings.checkpoint_database_url`` with ``thread_id = str(run_id)``, so a run
started by one process (graph A) can be resumed by a later, unrelated process
(graph B) against the same Postgres database. Because the already-completed
upstream events live inside the checkpoint's channel state, resuming never
re-emits or re-persists them — which is what the cross-instance recovery test
pins down.

Run/event rows live in the relational schema and are independent of the
Postgres checkpoint; the executor persists events keyed by ``run_id`` and
deduplicates by reading the checkpoint state for an interrupted run.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import selectors
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError

from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.config import Settings, get_settings
from bidscope.observability import METRICS_REGISTRY
from bidscope.persistence.models import QueryRun, RunEvent

logger = logging.getLogger(__name__)


def _to_datetime(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp from a node event, falling back to now."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def _to_plain_dsn(url: str) -> str:
    """Strip the SQLAlchemy ``postgresql+psycopg://`` driver qualifier.

    :class:`~langgraph.checkpoint.postgres.aio.AsyncPostgresSaver` consumes a
    plain libpq/psycopg DSN, while the app ships the SQLAlchemy-qualified form.
    """
    return re.sub(r"^postgresql\+psycopg://", "postgresql://", url)


def _selector_event_loop() -> asyncio.AbstractEventLoop:
    """Build a ``SelectorEventLoop``.

    psycopg's async connection requires a selector-backed loop; Windows ships
    with the proactor default, so we switch explicitly. Both the CLI and
    integration tests run their coroutine via
    ``asyncio.run(main, loop_factory=_selector_event_loop)`` so the checkpointer
    works regardless of platform defaults.
    """
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def _run_async(coro: Any) -> Any:
    """Run ``coroutine`` on a selector-backed event loop (Windows-safe)."""
    return asyncio.run(coro, loop_factory=_selector_event_loop)


def _thread_id(run_id: str) -> str:
    return str(run_id)


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": str(thread_id)}}


async def setup_checkpoints(settings: Settings) -> None:
    """Create the LangGraph checkpoint tables. CLI-only; not called implicitly."""
    dsn = _to_plain_dsn(settings.checkpoint_database_dsn())
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()


class EventReconciliationError(RuntimeError):
    """A checkpoint cannot be safely aligned with its relational event attempt."""


class RunOwnershipLostError(RuntimeError):
    """The worker no longer owns the relational run row."""


_VALID_NODE_NAMES = frozenset({"intent", "duplicate", "retrieval", "report", "persist", "unknown"})


def _resolve_node_name(name: str) -> str:
    """Map a graph node name to the valid metric label vocabulary."""
    return name if name in _VALID_NODE_NAMES else "unknown"


def _error_code_for(exc: BaseException) -> str:
    """Derive a ``bidscope_run_failures_total`` error code from an exception."""
    if isinstance(exc, RunOwnershipLostError):
        return "ownership_lost"
    if isinstance(exc, EventReconciliationError):
        return "graph_node_error"
    return "unknown"


CheckpointWriteGuard = Callable[[], Awaitable[None]]
_checkpoint_write_guard: ContextVar[CheckpointWriteGuard | None] = ContextVar(
    "bidscope_checkpoint_write_guard",
    default=None,
)


class FencedCheckpointSaver(BaseCheckpointSaver[Any]):
    """Delegate async checkpoint I/O while fencing every mutation by run ownership."""

    def __init__(self, delegate: BaseCheckpointSaver[Any]) -> None:
        super().__init__(serde=delegate.serde)
        self.delegate = delegate

    @property
    def config_specs(self) -> list[Any]:
        return self.delegate.config_specs

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.delegate.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self.delegate.list(config, filter=filter, before=before, limit=limit)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self.delegate.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for checkpoint in self.delegate.alist(
            config,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield checkpoint

    async def _ensure_write_allowed(self) -> None:
        guard = _checkpoint_write_guard.get()
        if guard is None:
            raise RunOwnershipLostError("checkpoint mutation requires active run ownership")
        await guard()

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        await self._ensure_write_allowed()
        return await self.delegate.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._ensure_write_allowed()
        await self.delegate.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._ensure_write_allowed()
        await self.delegate.adelete_thread(thread_id)

    def get_next_version(self, current: Any | None, channel: None) -> Any:
        return self.delegate.get_next_version(current, channel)


def run_lock_key(run_id: str) -> int:
    """Derive a deterministic signed 64-bit advisory lock key for one run."""
    digest = hashlib.sha256(f"run::{run_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _invalidate_connection(connection: Any, error: BaseException) -> None:
    """Retire or close a connection after advisory-lock cleanup is uncertain."""
    invalidate = getattr(connection, "invalidate", None)
    if invalidate is not None:
        try:
            result = invalidate(error)
            if inspect.isawaitable(result):
                await result
            return
        except BaseException:
            pass

    close = getattr(connection, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


async def _acquire_run_lock(connection: Any, run_id: str) -> bool:
    """Try a session-level run lock and finish its transaction immediately."""
    try:
        result = await connection.execute(
            sa.text("SELECT pg_try_advisory_lock(:key)"),
            {"key": run_lock_key(run_id)},
        )
        acquired = bool(result.scalar_one())
        if acquired:
            await connection.commit()
        else:
            await connection.rollback()
        return acquired
    except BaseException as error:
        with suppress(BaseException):
            await _invalidate_connection(connection, error)
        raise


async def _release_run_lock(connection: Any, run_id: str) -> None:
    """Unlock a run and commit, invalidating the connection on any failure."""
    try:
        result = await connection.execute(
            sa.text("SELECT pg_advisory_unlock(:key)"),
            {"key": run_lock_key(run_id)},
        )
        if not bool(result.scalar_one()):
            raise RuntimeError(f"advisory lock was not held at release: {run_id}")
        await connection.commit()
    except BaseException as error:
        with suppress(BaseException):
            await _invalidate_connection(connection, error)
        raise


async def _event_rows(
    run_id: str,
    session_factory: Any,
    *,
    seq_start: int | None = None,
    seq_end: int | None = None,
) -> list[RunEvent]:
    """Load ordered event rows, optionally bounded to an attempt sequence range."""
    statement = sa.select(RunEvent).where(RunEvent.query_run_id == str(run_id))
    if seq_start is not None:
        statement = statement.where(RunEvent.seq >= seq_start)
    if seq_end is not None:
        statement = statement.where(RunEvent.seq <= seq_end)
    statement = statement.order_by(RunEvent.seq)
    async with session_factory() as session:
        return list((await session.scalars(statement)).all())


async def _max_event_seq(run_id: str, session_factory: Any) -> int | None:
    """Return the highest relational event sequence for gap detection."""
    async with session_factory() as session:
        maximum = await session.scalar(
            sa.select(sa.func.max(RunEvent.seq)).where(RunEvent.query_run_id == str(run_id))
        )
    return int(maximum) if maximum is not None else None


def _event_fingerprint(event: Any) -> tuple[Any, ...]:
    """Return timestamp-independent identity for a streamed or stored event."""
    if isinstance(event, dict):
        return (
            event.get("node", ""),
            event.get("event", ""),
            event.get("status", ""),
            event.get("message"),
            event.get("details", {}),
        )
    return (
        event.node,
        event.event,
        event.status,
        event.message,
        event.details,
    )


async def _reconcile_event_cursor(
    run_id: str,
    checkpoint_events: list[dict[str, Any]],
    session_factory: Any,
    *,
    event_seq_offset: int | None = None,
    require_complete: bool = False,
) -> tuple[int, int]:
    """Align local events to exact relational sequences without history guessing.

    New checkpoints carry an attempt base. Legacy checkpoints without one are
    accepted only when their local events are an exact prefix of sequences
    ``0..N-1``; any other shape raises rather than selecting a suffix from a
    different attempt.
    """
    if event_seq_offset is None:
        if not checkpoint_events:
            if await _max_event_seq(run_id, session_factory) is not None:
                raise EventReconciliationError(
                    "legacy checkpoint has no events but relational history is non-empty"
                )
            return 0, 0
        local = [_event_fingerprint(event) for event in checkpoint_events]
        rows = await _event_rows(
            run_id,
            session_factory,
            seq_start=0,
            seq_end=len(local) - 1,
        )
        by_seq = {row.seq: row for row in rows}
        for index, fingerprint in enumerate(local):
            row = by_seq.get(index)
            if row is None:
                raise EventReconciliationError(
                    f"legacy checkpoint is missing relational sequence {index}"
                )
            if _event_fingerprint(row) != fingerprint:
                raise EventReconciliationError(
                    f"legacy checkpoint conflicts at relational sequence {index}"
                )
        maximum = await _max_event_seq(run_id, session_factory)
        if maximum is not None and maximum >= len(local):
            raise EventReconciliationError(
                f"legacy relational event history has trailing sequence {len(local)}"
            )
        return len(local), 0

    if event_seq_offset < 0:
        raise EventReconciliationError("event sequence offset must be non-negative")

    local = [_event_fingerprint(event) for event in checkpoint_events]
    rows = await _event_rows(
        run_id,
        session_factory,
        seq_start=event_seq_offset,
    )
    by_seq = {row.seq: row for row in rows}
    if by_seq:
        expected_sequences = list(range(event_seq_offset, max(by_seq) + 1))
        missing = next((seq for seq in expected_sequences if seq not in by_seq), None)
        if missing is not None:
            raise EventReconciliationError(
                f"relational event history skips expected sequence {missing}"
            )

    matched = 0
    for index, fingerprint in enumerate(local):
        expected_seq = event_seq_offset + index
        row = by_seq.get(expected_seq)
        if row is None:
            if require_complete:
                raise EventReconciliationError(
                    f"relational event history is missing expected sequence {expected_seq}"
                )
            break
        if _event_fingerprint(row) != fingerprint:
            raise EventReconciliationError(
                f"checkpoint event conflicts at relational sequence {expected_seq}"
            )
        matched += 1

    if max(by_seq, default=event_seq_offset - 1) >= event_seq_offset + len(local):
        raise EventReconciliationError(
            f"relational event history has trailing sequence {event_seq_offset + len(local)}"
        )
    return matched, event_seq_offset


async def _ensure_active(
    ensure_active: Callable[[Any | None], Awaitable[None]] | None,
    session: Any | None = None,
) -> None:
    """Invoke ownership fencing callbacks with legacy no-argument compatibility."""
    if ensure_active is None:
        return
    if len(inspect.signature(ensure_active).parameters) == 0:
        await ensure_active()  # type: ignore[call-arg]
    else:
        await ensure_active(session)


async def _next_event_seq(run_id: str, session_factory: Any) -> int:
    """Return the next relational sequence for a fresh checkpoint attempt."""
    async with session_factory() as session:
        maximum = await session.scalar(
            sa.select(sa.func.max(RunEvent.seq)).where(RunEvent.query_run_id == str(run_id))
        )
    return int(maximum) + 1 if maximum is not None else 0


async def _assert_execution_token(
    run_id: str,
    session_factory: Any,
    execution_token: str | None,
) -> None:
    """Reject calls that omit, outlive, or mismatch a committed ownership token."""
    async with session_factory() as session:
        ownership = (
            await session.execute(
                sa.select(QueryRun.execution_token, QueryRun.status).where(
                    QueryRun.id == str(run_id)
                )
            )
        ).one_or_none()
    if ownership is None:
        raise RunOwnershipLostError(f"run ownership lost: {run_id}")
    current_token, current_status = ownership
    if execution_token is None:
        active = (
            current_token is None and current_status not in ("running", "retryable")
        )
    else:
        active = current_token == execution_token and current_status == "running"
    if not active:
        raise RunOwnershipLostError(f"run ownership lost: {run_id}")


async def execute(
    graph: Any,
    run_id: str,
    input: Any,
    *,
    session_factory: Any,
    checkpoint_thread_id: str | None = None,
    force_fresh: bool = False,
    ensure_active: Callable[[Any | None], Awaitable[None]] | None = None,
    execution_token: str | None = None,
) -> dict[str, Any]:
    """Drive ``graph`` from ``input`` and persist each new node event.

    ``input`` is a ``user_request`` dict for a fresh run or a
    :class:`~langgraph.types.Command` for a resume. The executor reads the
    checkpoint state (if any) to learn how many node events are already stored,
    then persists only the events that appear beyond that point — so a resumed
    run never duplicates upstream events.

    Idempotency: if the graph has already reached a terminal state for this
    ``run_id``, the checkpointer holds the completed state and the function
    returns immediately — re-invoking with the same input never duplicates
    ``run_events`` rows. ``force_fresh=True`` explicitly bypasses that guard
    for a retry that must execute the original request again.
    """
    config = _config(checkpoint_thread_id or run_id)
    if isinstance(input, dict) and "run_id" not in input:
        input = {**input, "run_id": str(run_id)}
    if force_fresh and execution_token is None:
        raise RunOwnershipLostError(f"run ownership lost: {run_id}")

    await _assert_execution_token(run_id, session_factory, execution_token)
    existing = await graph.aget_state(config)

    async def ensure_checkpoint_write_allowed() -> None:
        await _assert_execution_token(run_id, session_factory, execution_token)
        await _ensure_active(ensure_active)

    fresh_offset: int | None = None
    if force_fresh:
        await _ensure_active(ensure_active)
        fresh_offset = await _next_event_seq(run_id, session_factory)
        write_guard_token = _checkpoint_write_guard.set(ensure_checkpoint_write_allowed)
        try:
            await _reset_checkpoint_state(graph, config)
        finally:
            _checkpoint_write_guard.reset(write_guard_token)

    checkpoint_values = (existing.values or {}) if existing else {}
    checkpoint_events = [] if force_fresh else list(checkpoint_values.get("node_events", []))
    event_seq_offset = (
        fresh_offset
        if force_fresh
        else checkpoint_values.get("event_seq_offset")
    )
    if event_seq_offset is None and not checkpoint_values:
        event_seq_offset = 0
    if isinstance(input, dict):
        input = {**input, "event_seq_offset": event_seq_offset}

    persisted, persisted_seq_offset = await _reconcile_event_cursor(
        run_id,
        checkpoint_events,
        session_factory,
        event_seq_offset=event_seq_offset,
        require_complete=bool(
            not force_fresh and existing and existing.values and not existing.next
        ),
    )

    if not force_fresh and existing and existing.values and not existing.next:
        await _ensure_active(ensure_active)
        return dict(existing.values)

    await _ensure_active(ensure_active)

    write_guard_token = _checkpoint_write_guard.set(ensure_checkpoint_write_allowed)
    _node_timer_start = time.monotonic()
    try:
        try:
            async for state in graph.astream(input, config, stream_mode="values"):
                _node_elapsed = time.monotonic() - _node_timer_start
                _node_timer_start = time.monotonic()
                events = state.get("node_events", [])
                new_count = len(events)
                if new_count > persisted:
                    _latest_node = (
                        events[new_count - 1].get("node", "unknown") if events else "unknown"
                    )
                    try:
                        METRICS_REGISTRY.observe(
                            "bidscope_run_node_duration_seconds",
                            min(max(_node_elapsed, 0.0), 3600.0),
                            {"node": _resolve_node_name(str(_latest_node))},
                        )
                    except Exception:
                        logger.warning("metrics_node_duration_failed", exc_info=True)
                    await _append_events(
                        run_id,
                        events,
                        persisted,
                        session_factory,
                        seq_offset=persisted_seq_offset,
                        ensure_active=ensure_active,
                        execution_token=execution_token,
                    )
                    persisted = new_count
        except BaseException as exc:
            try:
                METRICS_REGISTRY.counter(
                    "bidscope_run_failures_total",
                    {"error_code": _error_code_for(exc)},
                )
            except Exception:
                logger.warning("metrics_run_failure_counter_failed", exc_info=True)
            raise
    finally:
        _checkpoint_write_guard.reset(write_guard_token)

    final = await graph.aget_state(config)
    return dict(final.values) if final else {}


async def _reset_checkpoint_state(
    graph: Any,
    config: RunnableConfig,
) -> bool:
    """Clear one thread's checkpoint while leaving relational run events intact."""
    checkpointer = getattr(graph, "checkpointer", None)
    delete_thread = getattr(checkpointer, "adelete_thread", None)
    if delete_thread is None:
        return False
    await delete_thread(config["configurable"]["thread_id"])
    return True


async def _append_events(
    run_id: str,
    events: list[dict[str, Any]],
    start: int,
    session_factory: Any,
    *,
    seq_offset: int = 0,
    ensure_active: Callable[[Any | None], Awaitable[None]] | None = None,
    execution_token: str | None = None,
) -> None:
    """Persist ``events[start:]`` and heartbeat the owning query run."""
    if start >= len(events):
        return
    async with session_factory() as session:
        ownership = (
            await session.execute(
                sa.select(QueryRun.execution_token, QueryRun.status).where(
                    QueryRun.id == str(run_id)
                )
            )
        ).one_or_none()
        if ownership is None:
            await session.rollback()
            raise RunOwnershipLostError(f"run ownership lost: {run_id}")
        current_token, current_status = ownership
        if execution_token is None:
            active = current_token is None and current_status not in (
                "running",
                "retryable",
            )
        else:
            ownership = await session.execute(
                sa.update(QueryRun)
                .where(
                    QueryRun.id == str(run_id),
                    QueryRun.status == "running",
                    QueryRun.execution_token == execution_token,
                )
                .values(updated_at=datetime.now(UTC))
            )
            active = bool(getattr(ownership, "rowcount", 0))
        if not active:
            await session.rollback()
            raise RunOwnershipLostError(f"run ownership lost: {run_id}")
        await _ensure_active(ensure_active, session)
        for index in range(start, len(events)):
            event = events[index]
            session.add(
                RunEvent(
                    query_run_id=str(run_id),
                    seq=seq_offset + index,
                    timestamp=_to_datetime(event.get("timestamp")),
                    node=event.get("node", ""),
                    event=event.get("event", ""),
                    status=event.get("status", ""),
                    message=event.get("message"),
                    details=event.get("details", {}),
                )
            )
        await session.commit()


async def create_run(
    user_request: str,
    *,
    run_key: str | None = None,
    session_factory: Any,
    audit_context: Any | None = None,
) -> tuple[str, bool]:
    """Create or load a run keyed by ``run_key``.

    The unique ``run_key`` constraint is the concurrency boundary: if another
    caller wins the insert race, this function rolls back and loads that row.
    """
    import uuid

    resolved_key = run_key or str(uuid.uuid4())
    async with session_factory() as session:
        existing = await session.scalar(sa.select(QueryRun).where(QueryRun.run_key == resolved_key))
        if existing is not None:
            return str(existing.id), False

        run_id = str(uuid.uuid4())
        session.add(
            QueryRun(
                id=run_id,
                run_key=resolved_key,
                status="pending",
                user_request=user_request,
                checkpoint_thread_id=_thread_id(run_id),
            )
        )
        if audit_context is not None:
            await record_audit_event(
                session,
                AuditContext(
                    request_id=audit_context.request_id,
                    method=audit_context.method,
                    path=audit_context.path,
                    run_id=run_id,
                    subscription_id=audit_context.subscription_id,
                    report_id=audit_context.report_id,
                    snapshot_import_id=audit_context.snapshot_import_id,
                    error_code=audit_context.error_code,
                ),
                AuditEventType.RUN_CREATED,
                AuditOutcome.SUCCESS,
                {"status": "pending"},
            )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                sa.select(QueryRun).where(QueryRun.run_key == resolved_key)
            )
            if existing is None:
                raise
            return str(existing.id), False
    return run_id, True


async def mark_stale_runs_retryable(
    *,
    session_factory: Any,
    stale_before: datetime | None = None,
) -> int:
    """Mark stale ``pending`` and ``running`` rows as ``retryable``.

    A process crash can leave rows stuck before or during execution; on startup
    we flip old rows in both states to ``retryable``. Their checkpoints remain
    intact.
    """
    async with session_factory() as session:
        pending_update = sa.update(QueryRun).where(QueryRun.status == "pending")
        running_select = sa.select(QueryRun.id).where(QueryRun.status == "running")
        if stale_before is not None:
            pending_update = pending_update.where(QueryRun.updated_at < stale_before)
            running_select = running_select.where(QueryRun.updated_at < stale_before)
        pending_result = await session.execute(
            pending_update.values(
                status="retryable",
                execution_token=None,
                updated_at=datetime.now(UTC),
            )
        )
        candidate_ids = [
            str(run_id) for run_id in (await session.scalars(running_select)).all()
        ]
        await session.commit()

    changed = int(getattr(pending_result, "rowcount", 0) or 0)
    for candidate_id in candidate_ids:
        async with session_factory() as candidate_session:
            connection = await candidate_session.connection()
            acquired = await _acquire_run_lock(connection, candidate_id)
            if not acquired:
                continue
            try:
                update = sa.update(QueryRun).where(
                    QueryRun.id == candidate_id,
                    QueryRun.status == "running",
                )
                if stale_before is not None:
                    update = update.where(QueryRun.updated_at < stale_before)
                result = await connection.execute(
                    update.values(
                        status="retryable",
                        execution_token=None,
                        updated_at=datetime.now(UTC),
                    )
                )
                await connection.commit()
                changed += int(getattr(result, "rowcount", 0) or 0)
            finally:
                await _release_run_lock(connection, candidate_id)
    return changed


def run_setup_checkpoints(settings: Settings) -> None:
    """Windows-safe wrapper around :func:`setup_checkpoints` for the CLI."""
    _run_async(setup_checkpoints(settings))


__all__ = [
    "Command",
    "create_run",
    "execute",
    "mark_stale_runs_retryable",
    "run_setup_checkpoints",
    "setup_checkpoints",
    "_selector_event_loop",
    "_to_plain_dsn",
]


# Keep references used defensively so import-time checks stay green.
_ = (selectors, get_settings, QueryRun)
