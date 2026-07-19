"""Cross-instance checkpoint persistence and recovery for the query workflow.

These tests prove that a run started by one process (graph A) can be resumed by
a later, unrelated process (graph B) against the same Postgres checkpoint
store. Because already-completed upstream events live inside the checkpoint's
channel state, resuming never re-emits or re-persists them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from bidscope.clock import FixedClock
from bidscope.config import get_settings
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.graph.executor import (
    _to_plain_dsn,
    create_run,
    execute,
)
from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
from bidscope.persistence.models import RunEvent
from bidscope.retrieval.search import RetrievalFilter, RetrievalResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command


def _build_deps() -> GraphDeps:
    class FakeSearcher:
        def __init__(self) -> None:
            self.search_count = 0

        async def search(
            self, query: str, filters: RetrievalFilter | None = None,
        ) -> RetrievalResult:
            self.search_count += 1
            return RetrievalResult(
                query=query, candidates=[], degraded_modes=[], filters_applied={},
            )

    return GraphDeps(
        intent_model=FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=FakeSearcher(),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: {},
    )


async def _new_checkpointer():
    settings = get_settings()
    return AsyncPostgresSaver.from_conn_string(_to_plain_dsn(settings.checkpoint_database_url))


@pytest.mark.asyncio
async def test_cross_instance_resume_does_not_duplicate_upstream_events(
    session_factory: Any,
) -> None:
    """Graph A runs to interrupt; graph B resumes; upstream events are not doubled."""
    run_id = await create_run("weekly sichuan servers", session_factory=session_factory)
    deps = _build_deps()
    request = "每周一上午 9 点，汇总近 7 天四川省的服务器招标"
    user_request = {"user_request": request}

    # --- Graph A: run to the confirmation interrupt -------------------------
    async with await _new_checkpointer() as checkpointer_a:
        await checkpointer_a.setup()
        graph_a = build_graph(deps, checkpointer=checkpointer_a)
        interrupted = await execute(graph_a, run_id, user_request, session_factory=session_factory)
        assert interrupted.get("status") == "awaiting_confirmation"
        events_after_a = await _count_events(run_id, session_factory)
        assert events_after_a > 0

    # --- Graph B: a brand-new checkpointer/process resumes the same run -------
    async with await _new_checkpointer() as checkpointer_b:
        await checkpointer_b.setup()
        graph_b = build_graph(deps, checkpointer=checkpointer_b)
        finished = await execute(
            graph_b, run_id, Command(resume={"action": "approve"}), session_factory=session_factory
        )
        assert finished.get("status") == "completed"

    # Upstream events (parse_intent, validate_intent, confirm_intent) must not
    # be re-persisted by graph B.
    events_after_b = await _count_events(run_id, session_factory)
    nodes = await _persisted_nodes(run_id, session_factory)
    from collections import Counter
    duplicates = {node: n for node, n in Counter(nodes).items() if n > 1}
    assert not duplicates, f"upstream events duplicated: {duplicates}"
    # Graph B added downstream events but did not re-add the upstream ones.
    assert events_after_b >= events_after_a


async def _count_events(run_id: str, session_factory: Any) -> int:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count()).where(RunEvent.query_run_id == run_id)
        )
        return result.scalar_one() or 0


async def _persisted_nodes(run_id: str, session_factory: Any) -> list[str]:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(RunEvent.node).where(RunEvent.query_run_id == run_id).order_by(RunEvent.seq)
        )
        return [row[0] for row in result.all()]
