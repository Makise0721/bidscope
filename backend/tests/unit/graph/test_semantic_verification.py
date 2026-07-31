"""Graph-level coverage for the Semantic Citation Contract verifier node.

``verify_semantics`` is a soft gate: it collects verdicts for each claim using
only the claim's own cited evidence and hands them to the persistence node —
it never blocks or retries the run. A missing verifier or a failing verifier
call degrades gracefully (empty verdict set) instead of failing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bidscope.clock import FixedClock
from bidscope.domain.enums import ClaimSupportStatus, RunStatus
from bidscope.domain.intents import SearchIntent
from bidscope.evidence.fake_verifier import FakeSemanticVerifier
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.llm.types import ModelUsage, ReportDraft, VerifiedOpportunity
from bidscope.retrieval.search import (
    RetrievalFilter,
    RetrievalResult,
)
from graph_fakes import FakeReportPersistence
from langgraph.checkpoint.memory import InMemorySaver


class _ScriptedIntentModel:
    async def parse(self, request: str, clock: Any) -> SearchIntent:
        return SearchIntent(
            topics=["服务器"], expanded_terms=["服务器"], regions=["四川"],
            published_from=datetime(2026, 7, 11, tzinfo=UTC),
            published_to=datetime(2026, 7, 18, tzinfo=UTC),
            confidence=1.0,
        )

    @property
    def last_usage(self) -> ModelUsage | None:
        return None


class _ScriptedDuplicateModel:
    async def classify(self, pair: Any) -> Any:
        from bidscope.retrieval.deduplication import DuplicateClassification
        return DuplicateClassification(decision="ambiguous", reasons=("scripted",))

    @property
    def last_usage(self) -> ModelUsage | None:
        return None


class _ScriptedSearcher:
    async def search(self, query: str, filters: RetrievalFilter | None = None) -> RetrievalResult:
        from bidscope.retrieval.search import RetrievalCandidate as Candidate

        return RetrievalResult(
            query=query,
            candidates=[Candidate(notice_version_id="v1", score=1.0)],
            degraded_modes=[],
            filters_applied={},
        )


class _ScriptedReportModel:
    """Quotes the verified evidence verbatim — matches the fake verifier. """

    async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft:
        from bidscope.domain.reports import ReportCitation, ReportClaim, ReportItem

        claims = [
            ReportClaim(text=span.text, citation_ids=[span.evidence_id])
            for span in verified.evidence
        ]
        citations = [
            ReportCitation(evidence_id=span.evidence_id, label=span.evidence_id)
            for span in verified.evidence
        ]
        return ReportDraft(items=[
            ReportItem(
                notice_id=verified.notice_id,
                title=verified.title,
                known_fields={},
                unknown_fields=[],
                claims=claims,
                citations=citations,
            )
        ])

    @property
    def last_usage(self) -> ModelUsage | None:
        return None


class _ExplodingVerifier:
    """A verifier whose every call fails — the node must degrade, not fail."""

    async def verify(self, claim: Any, evidence: Any, *, evidence_ids: Any = None) -> Any:
        raise RuntimeError("provider unavailable")


def _deps(semantic_verifier: Any | None = None) -> GraphDeps:
    from bidscope.retrieval.deduplication import NoticeView

    views = {
        "v1": NoticeView(
            source="synthetic_demo",
            external_id="demo-001",
            canonical_url="https://example.invalid/demo-001",
            project_number="demo-pn-001",
            content_hash="a" * 64,
            title="四川省智算中心服务器采购项目公开招标公告",
            purchaser="四川省大数据中心",
            region="四川省",
            claim_supporting_texts=("预算金额：680万元。",),
        ),
    }
    persistence = FakeReportPersistence()
    return GraphDeps(
        intent_model=_ScriptedIntentModel(),
        duplicate_model=_ScriptedDuplicateModel(),
        report_model=_ScriptedReportModel(),
        searcher=_ScriptedSearcher(),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: views,
        report_persistence=persistence,
        semantic_verifier=semantic_verifier,
    ), persistence


async def test_verify_semantics_collects_verdicts_and_stamps_claims() -> None:
    """A wired verifier produces verdicts and the persisted report carries them."""
    deps, persistence = _deps(semantic_verifier=FakeSemanticVerifier())
    graph = build_graph(deps, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"user_request": "x", "run_id": "semantic-1"},
        {"configurable": {"thread_id": "semantic-1"}},
    )

    assert result["status"] == RunStatus.COMPLETED
    assert [v.verification.status for v in result["claim_verifications"]] == [
        ClaimSupportStatus.SUPPORTED
    ]
    persisted = persistence.persisted[-1].report
    assert persisted.items[0].claims[0].support_status == ClaimSupportStatus.SUPPORTED
    events = [event["event"] for event in result["node_events"]]
    assert "semantics_verified" in events


async def test_verify_semantics_without_verifier_is_skipped() -> None:
    """No verifier wired → empty verdict set, run still completes."""
    deps, persistence = _deps(semantic_verifier=None)
    graph = build_graph(deps, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"user_request": "x", "run_id": "semantic-none"},
        {"configurable": {"thread_id": "semantic-none"}},
    )

    assert result["status"] == RunStatus.COMPLETED
    assert result["claim_verifications"] == []
    persisted = persistence.persisted[-1].report
    assert persisted.items[0].claims[0].support_status is None
    assert "semantics_skipped" in [event["event"] for event in result["node_events"]]


async def test_verify_semantics_degrades_when_verifier_fails() -> None:
    """A failing verifier degrades (empty verdicts + degraded mode), never fails."""
    deps, _persistence = _deps(semantic_verifier=_ExplodingVerifier())
    graph = build_graph(deps, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"user_request": "x", "run_id": "semantic-degraded"},
        {"configurable": {"thread_id": "semantic-degraded"}},
    )

    assert result["status"] == RunStatus.COMPLETED
    assert result["claim_verifications"] == []
    assert "semantic_verifier_unavailable" in result["degraded_modes"]
    assert "semantics_degraded" in [event["event"] for event in result["node_events"]]
