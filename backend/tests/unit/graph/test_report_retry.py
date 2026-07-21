"""Graph-level test for the evidence-first report retry boundary.

The spec requires that a report-validation failure retries synthesis at most
once — and never re-runs retrieval, so the retrieval call count is invariant
(at exactly one). A second validation failure must fail the run as
``EvidenceInsufficient`` rather than delivering an unsupported report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bidscope.clock import FixedClock
from bidscope.domain.enums import RunStatus
from bidscope.domain.intents import SearchIntent
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.llm.ports import ReportModel
from bidscope.llm.types import ModelUsage, ReportDraft, VerifiedOpportunity
from bidscope.retrieval.search import (
    RetrievalCandidate,
    RetrievalFilter,
    RetrievalResult,
)
from langgraph.checkpoint.memory import InMemorySaver


class ScriptedIntentModel:
    """Returns a fixed, valid, high-confidence, non-scheduled intent."""

    def __init__(self, intent: SearchIntent) -> None:
        self._intent = intent

    async def parse(self, request: str, clock: Any) -> SearchIntent:
        return self._intent

    @property
    def last_usage(self) -> ModelUsage | None:
        return ModelUsage(
            model="scripted", prompt_tokens=0, completion_tokens=0,
            latency_ms=1.0, pricing_snapshot="offline",
        )


class ScriptedReportModel:
    """Fails validation for the first ``fail_for_first_n_calls``, then succeeds.

    A "failing" draft cites fabricated evidence that does not exist; a
    "passing" draft cites the verified opportunity's actual evidence, which
    ``verify_evidence`` registers in ``evidence_by_id``. This keeps the test
    independent of the exact evidence-id scheme.
    """

    def __init__(self, fail_for_first_n_calls: int) -> None:
        self.fail_for_first_n_calls = fail_for_first_n_calls
        self.call_count = 0

    def _draft(self, verified: VerifiedOpportunity, valid: bool) -> ReportDraft:
        from bidscope.domain.reports import ReportCitation, ReportClaim, ReportItem
        if valid:
            claims = [
                ReportClaim(text=span.text, citation_ids=[span.evidence_id])
                for span in verified.evidence
            ]
            citations = [
                ReportCitation(evidence_id=span.evidence_id, label=span.evidence_id)
                for span in verified.evidence
            ]
        else:
            claims = [ReportClaim(text="无来源声明", citation_ids=["ev-garbage"])]
            citations = [ReportCitation(evidence_id="ev-garbage")]
        item = ReportItem(
            notice_id=verified.notice_id,
            title=verified.title,
            known_fields={},
            unknown_fields=["budget"] if not valid else [],
            claims=claims,
            citations=citations,
        )
        return ReportDraft(items=[item], source_availability=["synthetic_demo"])

    async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft:
        invalid = self.call_count < self.fail_for_first_n_calls
        self.call_count += 1
        return self._draft(verified, valid=not invalid)

    @property
    def last_usage(self) -> ModelUsage | None:
        return ModelUsage(
            model="scripted", prompt_tokens=0, completion_tokens=0,
            latency_ms=1.0, pricing_snapshot="offline",
        )


class ScriptedDuplicateModel:
    async def classify(self, pair: Any) -> Any:
        from bidscope.retrieval.deduplication import DuplicateClassification
        return DuplicateClassification(decision="ambiguous", reasons=("scripted",))

    @property
    def last_usage(self) -> ModelUsage | None:
        return None


class ScriptedSearcher:
    """Returns a fixed candidate set and counts search invocations."""

    def __init__(self, candidate_ids: list[str], degraded: bool = False) -> None:
        self.candidate_ids = candidate_ids
        self.degraded = degraded
        self.search_count = 0

    async def search(self, query: str, filters: RetrievalFilter | None = None) -> RetrievalResult:
        self.search_count += 1
        candidates = [
            RetrievalCandidate(notice_version_id=i, score=1.0)
            for i in self.candidate_ids
        ]
        return RetrievalResult(
            query=query,
            candidates=candidates,
            degraded_modes=["vector_unavailable"] if self.degraded else [],
            filters_applied={},
        )


def _deps(
    *,
    fail_for_first_n_calls: int,
    candidate_ids: list[str] | None = None,
) -> GraphDeps:
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
    return GraphDeps(
        intent_model=ScriptedIntentModel(SearchIntent(
            topics=["服务器"], expanded_terms=["服务器"], regions=["四川"],
            published_from=datetime(2026, 7, 11, tzinfo=UTC),
            published_to=datetime(2026, 7, 18, tzinfo=UTC),
            confidence=1.0,
        )),
        duplicate_model=ScriptedDuplicateModel(),
        report_model=ScriptedReportModel(fail_for_first_n_calls),
        searcher=ScriptedSearcher(candidate_ids=candidate_ids or ["v1"]),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: views,
    )


async def test_retrieval_runs_once_even_with_synthesis_retry() -> None:
    """A validation failure retries synthesis once; retrieval is never re-run."""
    # Fail the first draft, pass on the retry.
    deps = _deps(fail_for_first_n_calls=1, candidate_ids=["v1"])
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "retry-1"}}

    result = await graph.ainvoke({"user_request": "x"}, config)

    assert result["status"] == RunStatus.COMPLETED
    assert deps.report_model.call_count == 2, "synthesis should run twice (initial + retry)"
    assert deps.searcher.search_count == 1, "retrieval must run exactly once"


async def test_second_validation_fails_as_evidence_insufficient() -> None:
    """Two validation failures give up — never deliver an unsupported report."""
    # Fail every draft so both the initial synthesis and the retry fail.
    deps = _deps(fail_for_first_n_calls=2, candidate_ids=["v1"])
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "retry-2"}}

    result = await graph.ainvoke({"user_request": "x"}, config)

    assert result["status"] == RunStatus.FAILED
    assert deps.report_model.call_count == 2, "synthesis should run twice then give up"
    assert deps.searcher.search_count == 1, "retrieval must run exactly once"
    codes = [e.code for e in result["errors"]]
    assert "evidence_insufficient" in codes


__all__ = ["ReportModel", "VerifiedOpportunity"]
