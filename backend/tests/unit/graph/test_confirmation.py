"""Interrupt-and-resume coverage for the confirmable query workflow.

The representative query is a *scheduled* (weekly) query, so ``confirm_intent``
MUST call ``interrupt()`` and the run pauses at ``awaiting_confirmation``.
Approving via ``Command(resume=...)`` resumes execution and completes the
first six nodes at ``candidates_resolved``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bidscope.clock import FixedClock
from bidscope.domain.enums import RunStatus
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
from bidscope.retrieval.search import (
    RetrievalCandidate,
    RetrievalFilter,
    RetrievalResult,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

REPRESENTATIVE_QUERY = (
    "每周一上午 9 点，汇总近 7 天四川和重庆与「智算中心、服务器」有关、"
    "预算 500 万以上的招标信息。"
)


class FakeHybridSearcher:
    """Returns a bounded, deterministic set of candidates without a database."""

    def __init__(self, candidate_ids: list[str], degraded: bool = False) -> None:
        self.candidate_ids = candidate_ids
        self.degraded = degraded
        self.search_count = 0
        self.last_filter: RetrievalFilter | None = None

    async def search(
        self, query: str, filters: RetrievalFilter | None = None
    ) -> RetrievalResult:
        self.search_count += 1
        self.last_filter = filters
        candidates = [
            RetrievalCandidate(notice_version_id=notice_id, score=1.0)
            for notice_id in self.candidate_ids
        ]
        degraded_modes = ["vector_unavailable"] if self.degraded else []
        return RetrievalResult(
            query=query,
            candidates=candidates,
            degraded_modes=degraded_modes,
            filters_applied={},
        )


def _notice_views() -> dict[str, object]:
    """Minimal notice views so the dedup/evidence pipeline has real input."""
    from bidscope.retrieval.deduplication import NoticeView

    return {
        "demo-001": NoticeView(
            source="synthetic_demo", external_id="demo-001",
            canonical_url="https://example.invalid/demo-001",
            project_number="SC-2026-9", content_hash="a" * 64,
            title="四川省智算中心服务器采购项目",
            purchaser="四川省大数据中心", region="四川省",
            budget_minor_units=6_800_000_00, budget_currency="CNY",
            claim_supporting_texts=("预算金额：680万元。",),
        ),
        "demo-002": NoticeView(
            source="synthetic_demo", external_id="demo-002",
            canonical_url="https://example.invalid/demo-002",
            project_number="CQ-2026-1", content_hash="b" * 64,
            title="重庆市服务器采购项目",
            purchaser="重庆市公共资源交易中心", region="重庆市",
            budget_minor_units=5_300_000_00, budget_currency="CNY",
            claim_supporting_texts=("预算金额：530万元。",),
        ),
    }


def _deps(
    *,
    candidate_ids: list[str] | None = None,
    degraded: bool = False,
) -> GraphDeps:
    return GraphDeps(
        intent_model=FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=FakeHybridSearcher(
            candidate_ids=candidate_ids or ["demo-001", "demo-002"], degraded=degraded
        ),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: _notice_views(),
    )


async def test_scheduled_query_interrupts_and_resumes() -> None:
    """A weekly query pauses for confirmation, then resolves candidates."""
    deps = _deps()
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-1"}}

    paused = await graph.ainvoke({"user_request": REPRESENTATIVE_QUERY}, config)

    assert paused["status"] == RunStatus.AWAITING_CONFIRMATION
    assert paused["search_intent"].schedule is not None
    # The run waits at confirmation: retrieval has not started yet.
    assert deps.searcher.search_count == 0
    assert paused["candidate_notice_ids"] == []

    # Resuming with the application-level approval continues the workflow
    # through retrieval, evidence binding, synthesis and delivery to completion.
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert resumed["status"] == RunStatus.COMPLETED
    assert resumed["search_intent"].schedule is not None
    assert resumed["candidate_notice_ids"]
    # Retrieval ran exactly once and resolved duplicates after it.
    assert deps.searcher.search_count == 1
    # With real notice views, evidence binding produced verified opportunities.
    assert len(resumed["verified_opportunities"]) > 0


async def test_pause_blocks_until_resume() -> None:
    """A run at confirmation does not advance to retrieval without a resume.

    Declining is handled at the application layer (which simply does not
    resume the graph); the graph itself waits. The paused run must not have
    started retrieval, and a ``Command(resume=...)`` is what advances it.
    """
    deps = _deps()
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-2"}}

    paused = await graph.ainvoke({"user_request": REPRESENTATIVE_QUERY}, config)
    assert paused["status"] == RunStatus.AWAITING_CONFIRMATION
    assert deps.searcher.search_count == 0
    # A resume command advances the run past confirmation to retrieval.
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config)
    assert resumed["status"] == RunStatus.COMPLETED
    assert deps.searcher.search_count == 1


def test_build_graph_accepts_in_memory_checkpointer() -> None:
    """``build_graph`` compiles to a runnable graph without a database."""
    graph = build_graph(_deps(), checkpointer=InMemorySaver())
    assert graph is not None
