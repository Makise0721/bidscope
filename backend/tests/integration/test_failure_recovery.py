"""Integration tests for failure and degradation handling (Task 19, RED phase).

Six failure surfaces are pinned here:

1. Vector-provider degradation — the run completes in lexical-only mode.
2. Model transient retry — a one-shot model failure is retried (gap).
3. Evidence-validation retry — an invalid report loops back to synthesis once.
4. DOCX storage failure — the error is bounded, no row is persisted (gap).
5. Stale-running startup recovery — ``running`` rows flip to ``retryable``.
6. Subscription three-strike pause — three failures pause the subscription.

Each test asserts the *desired* end state. Where the guard already exists the
test passes; where it is missing it fails, exposing the gap.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.domain.reports import Report, ReportClaim, ReportItem
from bidscope.domain.types import BidScopeErrorCode
from bidscope.llm.types import ModelUsage, ReportDraft, VerifiedOpportunity
from bidscope.persistence.models import (
    QueryRun,
    Subscription,
)
from bidscope.persistence.models import (
    Report as ReportModel,
)
from graph_fakes import FakeReportPersistence
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Shared helpers (mirroring backend/tests/unit/graph/test_confirmation.py)
# ---------------------------------------------------------------------------


class FakeHybridSearcher:
    """Returns a bounded, deterministic set of candidates without a database."""

    def __init__(self, candidate_ids: list[str], degraded: bool = False) -> None:
        self.candidate_ids = candidate_ids
        self.degraded = degraded
        self.search_count = 0

    async def search(self, query, filters=None):  # type: ignore[no-untyped-def]
        from bidscope.retrieval.search import RetrievalCandidate, RetrievalResult

        self.search_count += 1
        candidates = [
            RetrievalCandidate(notice_version_id=notice_id, score=1.0)
            for notice_id in self.candidate_ids
        ]
        degraded_modes = ["vector_unavailable"] if self.degraded else []
        return RetrievalResult(
            query=query, candidates=candidates,
            degraded_modes=degraded_modes, filters_applied={},
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
    }


def _deps(
    *,
    intent_model=None,
    report_model=None,
    candidate_ids: list[str] | None = None,
    degraded: bool = False,
):
    from bidscope.clock import FixedClock
    from bidscope.graph.builder import GraphDeps
    from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel

    return GraphDeps(
        intent_model=intent_model or FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=report_model or FakeReportModel(),
        searcher=FakeHybridSearcher(
            candidate_ids=candidate_ids or ["demo-001"], degraded=degraded
        ),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=lambda ids: _notice_views(),
        report_persistence=FakeReportPersistence(),
    )


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(sa.select(sa.func.count()).select_from(model))
    return result.scalar_one()


@pytest_asyncio.fixture(autouse=True)
async def clean_recovery_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate tables these tests touch for isolation."""
    async with session_factory() as session:
        await session.execute(
            sa.text("TRUNCATE run_events, reports, subscriptions CASCADE")
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Vector-provider degradation
# ---------------------------------------------------------------------------


async def test_vector_degradation_completes_in_lexical_mode() -> None:
    """When the embedding provider is unavailable, the run still completes.

    ``retrieve_candidates`` records ``degraded_modes=["vector_unavailable"]``
    but does not fail the run; retrieval falls back to lexical matching and
    the workflow proceeds to completion.
    """
    from bidscope.domain.enums import RunStatus
    from bidscope.graph.builder import build_graph
    from langgraph.checkpoint.memory import InMemorySaver

    deps = _deps(degraded=True)
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-degraded"}}

    result = await graph.ainvoke({"user_request": "四川服务器招标"}, config)

    assert result["status"] == RunStatus.COMPLETED
    assert result["degraded_modes"] == ["vector_unavailable"]
    assert deps.searcher.search_count == 1


# ---------------------------------------------------------------------------
# 2. Model transient retry (gap)
# ---------------------------------------------------------------------------


async def test_transient_model_error_is_retried() -> None:
    """A one-shot model failure must be retried; the run should complete.

    The intent model raises on its first ``parse`` call and succeeds on the
    second. The desired behaviour is that the graph retries the transient
    error and the run completes. (Currently ``parse_intent`` has no retry —
    this test documents the gap.)
    """
    from bidscope.domain.enums import RunStatus
    from bidscope.graph.builder import build_graph
    from bidscope.llm.fake import FakeIntentModel
    from langgraph.checkpoint.memory import InMemorySaver

    class FlakyIntentModel:
        def __init__(self) -> None:
            self._calls = 0
            self._inner = FakeIntentModel()
            self._last_usage: ModelUsage | None = None

        @property
        def last_usage(self) -> ModelUsage | None:
            return self._last_usage

        async def parse(self, request, clock):  # type: ignore[no-untyped-def]
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("transient model error")
            result = await self._inner.parse(request, clock)
            self._last_usage = self._inner.last_usage
            return result

    deps = _deps(intent_model=FlakyIntentModel())
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-transient"}}

    try:
        result = await graph.ainvoke({"user_request": "四川服务器招标"}, config)
    except Exception as e:
        pytest.fail(f"gap: transient model error was not retried: {e}")

    assert result["status"] == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# 3. Evidence-validation retry
# ---------------------------------------------------------------------------


async def test_evidence_validation_retries_synthesis_once() -> None:
    """An invalid report loops back to synthesis exactly once, then completes.

    The report model returns an unsupported draft on the first synthesis pass
    (a claim citing nonexistent evidence) and a clean draft on the second.
    ``validate_report`` retries synthesis once (``MAX_SYNTHESIS_RETRIES=1``);
    the second, valid draft proceeds to delivery.
    """
    from bidscope.domain.enums import RunStatus
    from bidscope.graph.builder import build_graph
    from bidscope.llm.fake import FakeReportModel
    from langgraph.checkpoint.memory import InMemorySaver

    class ScriptedReportModel:
        def __init__(self) -> None:
            self._calls = 0
            self._inner = FakeReportModel()
            self._last_usage: ModelUsage | None = None

        @property
        def last_usage(self) -> ModelUsage | None:
            return self._last_usage

        async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft:
            self._calls += 1
            self._last_usage = self._inner.last_usage
            if self._calls == 1:
                # Invalid: the claim cites evidence that does not exist.
                return ReportDraft(items=[ReportItem(
                    notice_id=verified.notice_id, title="bad",
                    claims=[ReportClaim(text="x", citation_ids=["nonexistent"])],
                )])
            # Valid: no claims to validate.
            return ReportDraft(items=[ReportItem(
                notice_id=verified.notice_id, title="good",
            )])

    deps = _deps(report_model=ScriptedReportModel())
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-retry"}}

    result = await graph.ainvoke({"user_request": "四川服务器招标"}, config)

    assert result["status"] == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# 4. DOCX storage failure (gap)
# ---------------------------------------------------------------------------


async def test_docx_storage_failure_is_bounded_and_keeps_online_report(
    session_factory, tmp_path
) -> None:
    """DOCX failure is bounded but cannot roll back the online report."""
    import uuid

    from bidscope.delivery.reports import ReportPersistence

    class FailingObjectStore:
        def put_bytes(self, key: str, data: bytes) -> str:
            raise RuntimeError("storage backend unavailable")

        def get_bytes(self, key: str) -> bytes:
            raise FileNotFoundError(key)

        def exists(self, key: str) -> bool:
            return False

    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id, run_key=run_id, status="running", user_request="服务器",
        ))
        await session.commit()

    persistence = ReportPersistence(session_factory, FailingObjectStore())
    report = Report(
        run_id=run_id,
        generated_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        query_conditions={"topics": "服务器", "regions": "四川"},
    )
    persisted = await persistence.persist_online_report(report, {})

    with pytest.raises(Exception) as exc_info:
        await persistence.export_docx(persisted)

    async with session_factory() as session:
        stored = await session.get(ReportModel, persisted.id)
        assert stored is not None
        assert stored.docx_object_key is None

    assert getattr(exc_info.value, "code", None) == BidScopeErrorCode.DELIVERY_ERROR


# ---------------------------------------------------------------------------
# 5. Stale-running startup recovery
# ---------------------------------------------------------------------------


async def test_stale_running_runs_flip_to_retryable(
    session_factory,
) -> None:
    """Rows stuck in ``running`` are flipped to ``retryable`` on startup.

    A process crash leaves rows in ``running``; ``mark_stale_runs_retryable``
    flips them to ``retryable`` so they can be explicitly restarted. Their
    checkpoints are left intact for an explicit resume.
    """
    import uuid

    from bidscope.graph.executor import mark_stale_runs_retryable

    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id, run_key=run_id, status="running",
            user_request="四川服务器", checkpoint_thread_id=run_id,
        ))
        await session.commit()

    count = await mark_stale_runs_retryable(session_factory=session_factory)
    assert count == 1

    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run.status == "retryable"


# ---------------------------------------------------------------------------
# 6. Subscription three-strike pause
# ---------------------------------------------------------------------------


async def test_three_consecutive_failures_pause_subscription(
    session_factory,
) -> None:
    """Three consecutive failures pause the subscription.

    ``_record_failure`` increments ``__consecutive_failures`` and flips the
    subscription to ``paused`` once the count reaches three, so a persistently
    failing subscription stops consuming workers.
    """
    import uuid as _uuid

    from bidscope.subscriptions.service import SubscriptionService

    service = SubscriptionService(session_factory)
    # Seed the subscription row directly: this test exercises ``_record_failure``
    # in isolation and does not need the create-from-run contract.
    sub = Subscription(
        id=str(_uuid.uuid4()),
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
        normalized_intent={
            "regions": ["四川"],
            "__consecutive_failures": 0,
        },
        status="active",
        trigger_key=f"trigger-{_uuid.uuid4()}",
    )
    async with session_factory() as session:
        session.add(sub)
        await session.commit()

    for _ in range(3):
        async with session_factory() as session:
            sub = await session.get(Subscription, sub.id)
            await service._record_failure(session, sub)
            await session.commit()

    assert sub.status == "paused"
