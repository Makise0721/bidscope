"""Routing tests for the first six graph nodes.

Beyond the happy-path confirmation, the graph must route deterministic signals
the right way:

* invalid dates / budgets / regions — rejected at ``validate_intent`` (no
  model call, no retrieval);
* an unresolved schedule with low confidence — still pauses for confirmation;
* an empty retrieval — a valid (non-error) terminal input for the next stage;
* vector-unavailable degradation — lexical results flow through with
  ``degraded_modes=['vector_unavailable']``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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


class ScriptedIntentModel(FakeIntentModel):
    """A fake intent model that returns a fully-scripted :class:`SearchIntent`."""

    def __init__(self, intent: Any) -> None:
        super().__init__()
        self._intent = intent

    async def parse(self, request: str, clock: Any) -> Any:
        from bidscope.llm.types import ModelUsage

        self._last_usage = ModelUsage(
            model="fake-deterministic",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=1.0,
            pricing_snapshot="offline",
        )
        return self._intent


class ScriptedSearcher:
    """Returns a pre-baked :class:`RetrievalResult` without a database."""

    def __init__(
        self,
        candidate_ids: list[str] | None = None,
        degraded: bool = False,
    ) -> None:
        self.candidate_ids = candidate_ids or []
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
        return RetrievalResult(
            query=query,
            candidates=candidates,
            degraded_modes=["vector_unavailable"] if self.degraded else [],
            filters_applied={},
        )


def _deps(
    *,
    intent: Any,
    candidate_ids: list[str] | None = None,
    degraded: bool = False,
) -> GraphDeps:
    return GraphDeps(
        intent_model=ScriptedIntentModel(intent),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=ScriptedSearcher(candidate_ids=candidate_ids, degraded=degraded),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: {},
    )


def _valid_intent(*, schedule: bool = False, confidence: float = 1.0) -> Any:
    from bidscope.domain.intents import RunSchedule, SearchIntent

    return SearchIntent(
        topics=["服务器"],
        expanded_terms=["服务器", "GPU 服务器"],
        regions=["四川"],
        published_from=datetime(2026, 7, 11, tzinfo=UTC),
        published_to=datetime(2026, 7, 18, tzinfo=UTC),
        min_budget=None,
        max_budget=None,
        schedule=(
            RunSchedule(cron_expression="0 9 * * 1", timezone="Asia/Shanghai") if schedule else None
        ),
        confidence=confidence,
        assumptions=[],
    )


async def test_empty_intent_is_rejected_at_validation() -> None:
    """An intent missing topics and regions is a deterministic validation failure.

    Unlike date/budget ordering (which the domain schema already rejects at
    construction), an *empty* ``topics``/``regions`` list passes the domain
    model but is semantically invalid — ``validate_intent`` is the gate that
    catches it and fails the run before any retrieval.
    """
    from bidscope.domain.intents import SearchIntent
    empty_intent = SearchIntent(topics=[], regions=[])
    deps = _deps(intent=empty_intent, candidate_ids=["demo-001"])
    graph = build_graph(deps, checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "r1"}}
    result = await graph.ainvoke({"user_request": "empty intent"}, config)

    assert result["status"] == RunStatus.FAILED
    assert result["errors"], "validation failure must record a typed error"
    assert deps.searcher.search_count == 0


async def test_low_confidence_non_scheduled_still_confirms() -> None:
    """A required-but-low-confidence field forces confirmation even unscheduled."""
    intent = _valid_intent(schedule=False, confidence=0.2)
    deps = _deps(intent=intent, candidate_ids=["demo-001"])
    graph = build_graph(deps, checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "r2"}}
    paused = await graph.ainvoke({"user_request": "low confidence"}, config)

    assert paused["status"] == RunStatus.AWAITING_CONFIRMATION
    # Resolving the confirmation completes the slice.
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config)
    assert resumed["status"] == RunStatus.CANDIDATES_RESOLVED


async def test_empty_retrieval_is_valid() -> None:
    """Zero candidates is a valid (non-error) result, not a system failure."""
    intent = _valid_intent(schedule=False, confidence=1.0)
    deps = _deps(intent=intent, candidate_ids=[], degraded=False)
    graph = build_graph(deps, checkpointer=InMemorySaver())

    # Approve any confirmation pause, then let the run finish.
    config = {"configurable": {"thread_id": "r3"}}
    first = await graph.ainvoke({"user_request": "empty retrieval"}, config)
    if first.get("__interrupt__"):
        first = await graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert first["status"] == RunStatus.CANDIDATES_RESOLVED
    assert first["candidate_notice_ids"] == []
    assert first["degraded_modes"] == []


async def test_vector_unavailable_degrades_lexically() -> None:
    """When embedding fails, lexical results flow through with a degraded flag."""
    intent = _valid_intent(schedule=False, confidence=1.0)
    deps = _deps(intent=intent, candidate_ids=["demo-001"], degraded=True)
    graph = build_graph(deps, checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "r4"}}
    first = await graph.ainvoke({"user_request": "degraded"}, config)
    if first.get("__interrupt__"):
        first = await graph.ainvoke(Command(resume={"action": "approve"}), config)

    assert first["status"] == RunStatus.CANDIDATES_RESOLVED
    assert first["candidate_notice_ids"] == ["demo-001"]
    assert first["degraded_modes"] == ["vector_unavailable"]
