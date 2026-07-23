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
import re
import selectors
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError

from bidscope.config import Settings, get_settings
from bidscope.persistence.models import QueryRun, RunEvent


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


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": _thread_id(run_id)}}


async def setup_checkpoints(settings: Settings) -> None:
    """Create the LangGraph checkpoint tables. CLI-only; not called implicitly."""
    dsn = _to_plain_dsn(settings.checkpoint_database_url)
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()


async def _persisted_event_count(run_id: str, session_factory: Any) -> int:
    """Return the number of ``run_events`` already stored for this run."""
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count()).where(RunEvent.query_run_id == str(run_id))
        )
        return result.scalar_one() or 0


async def execute(
    graph: Any,
    run_id: str,
    input: Any,
    *,
    session_factory: Any,
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
    ``run_events`` rows.
    """
    config = _config(run_id)
    if isinstance(input, dict) and "run_id" not in input:
        input = {**input, "run_id": str(run_id)}

    # Idempotency guard: a *completed* run short-circuits on the checkpointer.
    # A run interrupted at a pause node also carries a checkpoint, but its
    # ``next`` tuple names the still-pending node(s) — only a truly terminal
    # checkpoint (empty ``next``) is safe to return without re-streaming.
    existing = await graph.aget_state(config)
    if existing and existing.values and not existing.next:
        return dict(existing.values)

    already_persisted = await _persisted_event_count(run_id, session_factory)
    persisted = already_persisted

    async for state in graph.astream(input, config, stream_mode="values"):
        events = state.get("node_events", [])
        new_count = len(events)
        if new_count > persisted:
            await _append_events(run_id, events, persisted, session_factory)
            persisted = new_count

    final = await graph.aget_state(config)
    return dict(final.values) if final else {}


async def _append_events(
    run_id: str,
    events: list[dict[str, Any]],
    start: int,
    session_factory: Any,
) -> None:
    """Persist ``events[start:]`` with contiguous ``seq`` numbers."""
    if start >= len(events):
        return
    async with session_factory() as session:
        for index in range(start, len(events)):
            event = events[index]
            session.add(RunEvent(
                query_run_id=str(run_id),
                seq=index,
                timestamp=_to_datetime(event.get("timestamp")),
                node=event.get("node", ""),
                event=event.get("event", ""),
                status=event.get("status", ""),
                message=event.get("message"),
                details=event.get("details", {}),
            ))
        await session.commit()


async def create_run(
    user_request: str,
    *,
    run_key: str | None = None,
    session_factory: Any,
) -> tuple[str, bool]:
    """Create or load a run keyed by ``run_key``.

    The unique ``run_key`` constraint is the concurrency boundary: if another
    caller wins the insert race, this function rolls back and loads that row.
    """
    import uuid

    resolved_key = run_key or str(uuid.uuid4())
    async with session_factory() as session:
        existing = await session.scalar(
            sa.select(QueryRun).where(QueryRun.run_key == resolved_key)
        )
        if existing is not None:
            return str(existing.id), False

        run_id = str(uuid.uuid4())
        session.add(QueryRun(
            id=run_id,
            run_key=resolved_key,
            status="pending",
            user_request=user_request,
            checkpoint_thread_id=_thread_id(run_id),
        ))
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
) -> int:
    """Mark stale ``pending`` and ``running`` rows as ``retryable``.

    A process crash can leave rows stuck before or during execution; on startup
    we flip both states to ``retryable``. Their checkpoints remain intact.
    """
    async with session_factory() as session:
        result = await session.execute(
            sa.update(QueryRun)
            .where(QueryRun.status.in_(("pending", "running")))
            .values(status="retryable")
            .returning(QueryRun.id)
        )
        ids = result.scalars().all()
        await session.commit()
        return len(ids)


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
