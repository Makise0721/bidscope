"""Integration tests for hybrid tender retrieval.

The hybrid searcher applies structured filters (region, budget, publication
date) *before* any top-K recall, then fuses lexical (pg_trgm) and vector
(pgvector cosine) candidates with reciprocal-rank fusion. When the embedding
provider is unavailable, it degrades to lexical-only results.

Stored embeddings are controlled relative to the query embedding so vector
ranking is deterministic: an identical vector is maximally close (cosine
distance 0), a negated vector is maximally far (distance 2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.persistence.models import CanonicalNotice, NoticeVersion, SourceNotice
from bidscope.retrieval.embeddings import HashEmbeddingProvider
from bidscope.retrieval.search import (
    HybridSearcher,
    RetrievalFilter,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

QUERY_TEXT = "智算中心 服务器"
#: CNY 5,000,000 = 500万元 in integer minor units (分).
MIN_BUDGET_MINOR_UNITS = 500_000_000

#: Fixed reference time so retrieval-window logic is deterministic. The
#: "recent" fixture (2 days old) passes the 7-day filter; the "expired"
#: fixture (30 days old) is excluded — identical to the ``datetime.now()``
#: behaviour but stable across runs.
FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


async def _insert_notice(
    session: AsyncSession,
    *,
    title: str,
    region: str | None,
    budget_minor_units: int | None,
    publish_date: datetime | None,
    embedding: list[float] | None,
    source: str = "synthetic_demo",
    external_id: str = "ext",
) -> str:
    """Create a canonical/source notice + version triple and return the version id."""
    canonical = CanonicalNotice()
    session.add(canonical)
    await session.flush()

    source_notice = SourceNotice(
        canonical_notice_id=canonical.id,
        source=source,
        external_id=external_id,
        source_url="https://example.invalid/a.htm",
        first_seen_at=datetime(2026, 7, 1, tzinfo=UTC),
        latest_seen_at=datetime(2026, 7, 1, tzinfo=UTC),
        content_hash=external_id,
    )
    session.add(source_notice)
    await session.flush()

    version = NoticeVersion(
        source_notice_id=source_notice.id,
        payload_object_key=f"obj-{external_id}",
        capture_kind="synthetic_demo",
        parser_version="demo-v1",
        content_hash=external_id,
        title=title,
        region=region,
        publish_date=publish_date,
        budget_minor_units=budget_minor_units,
        budget_currency="CNY",
        embedding=embedding,
    )
    session.add(version)
    await session.flush()
    return str(version.id)


@pytest.fixture
def provider() -> HashEmbeddingProvider:
    return HashEmbeddingProvider()


@pytest_asyncio.fixture
async def query_embedding(provider: HashEmbeddingProvider) -> list[float]:
    """The exact embedding the searcher will compute for QUERY_TEXT."""
    return (await provider.embed([QUERY_TEXT]))[0]


@pytest_asyncio.fixture
async def seeded_session(
    session_factory: async_sessionmaker[AsyncSession],
    query_embedding: list[float],
) -> None:
    """Insert a controlled set of notices exercising every spec scenario."""
    now = FIXED_NOW
    recent = now - timedelta(days=2)
    expired = now - timedelta(days=30)
    # Maximally distant vector (negated query → cosine distance 2).
    far_vector = [-value for value in query_embedding]

    async with session_factory() as session:
        # 1. Sichuan + matching budget/time + strong vector: should rank high.
        await _insert_notice(
            session,
            title="四川省智算中心服务器采购项目",
            region="四川省",
            budget_minor_units=680_000_000,
            publish_date=recent,
            embedding=query_embedding,
            external_id="sichuan-strong",
        )
        # 2. Chongqing + matching budget/time + strong vector: should rank.
        await _insert_notice(
            session,
            title="重庆市服务器扩容项目",
            region="重庆市",
            budget_minor_units=530_000_000,
            publish_date=recent,
            embedding=query_embedding,
            external_id="chongqing-strong",
        )
        # 3. Region mismatch but text identical: EXCLUDED by region filter.
        await _insert_notice(
            session,
            title="四川省智算中心服务器采购项目",
            region="北京市",
            budget_minor_units=680_000_000,
            publish_date=recent,
            embedding=query_embedding,
            external_id="region-mismatch",
        )
        # 4. Under budget: EXCLUDED by budget filter.
        await _insert_notice(
            session,
            title="四川省智算中心服务器采购",
            region="四川省",
            budget_minor_units=200_000_000,
            publish_date=recent,
            embedding=query_embedding,
            external_id="under-budget",
        )
        # 5. Expired date: EXCLUDED by date filter.
        await _insert_notice(
            session,
            title="重庆市智算中心服务器采购",
            region="重庆市",
            budget_minor_units=600_000_000,
            publish_date=expired,
            embedding=query_embedding,
            external_id="expired-date",
        )
        # 6. Structure-passing, text-weak but vector-strong.
        await _insert_notice(
            session,
            title="某单位日常采购项目公告",
            region="四川省",
            budget_minor_units=600_000_000,
            publish_date=recent,
            embedding=query_embedding,
            external_id="text-weak-vector-strong",
        )
        # 7. Structure-passing, text-strong but vector-weak.
        await _insert_notice(
            session,
            title="智算中心服务器采购",
            region="四川省",
            budget_minor_units=600_000_000,
            publish_date=recent,
            embedding=far_vector,
            external_id="text-strong-vector-weak",
        )
        await session.commit()


@pytest.fixture
def searcher(provider: HashEmbeddingProvider, session_factory) -> HybridSearcher:
    return HybridSearcher(session_factory=session_factory, provider=provider, top_k=10)


def _fixed_filter() -> RetrievalFilter:
    now = FIXED_NOW
    return RetrievalFilter(
        regions=["四川省", "重庆市"],
        published_from=now - timedelta(days=7),
        published_to=now,
        min_budget_minor_units=MIN_BUDGET_MINOR_UNITS,
    )


@pytest.mark.asyncio
async def test_structured_filter_before_ranking(
    seeded_session, searcher, session_factory
) -> None:
    """Structured filters must exclude records before any ranking."""
    result = await searcher.search(QUERY_TEXT, _fixed_filter())
    returned_ids = [c.notice_version_id for c in result.candidates]

    async with session_factory() as session:
        expected = await _expected_passing_ids(session)
        excluded = await _expected_excluded_ids(session)

    assert sorted(returned_ids) == sorted(expected), (
        "only structure-passing records should be returned"
    )
    assert not any(excluded_id in returned_ids for excluded_id in excluded), (
        "region/budget/date violations must be excluded"
    )


@pytest.mark.asyncio
async def test_normalized_region_filter_matches_canonical_snapshot_region(
    seeded_session, searcher, session_factory
) -> None:
    """A province shorthand from intent parsing matches canonical snapshot data."""
    filters = _fixed_filter()
    filters.regions = ["四川"]
    result = await searcher.search(QUERY_TEXT, filters)

    async with session_factory() as session:
        expected = [
            await _id_for(session, "sichuan-strong"),
            await _id_for(session, "text-weak-vector-strong"),
            await _id_for(session, "text-strong-vector-weak"),
        ]

    actual = sorted(candidate.notice_version_id for candidate in result.candidates)
    assert actual == sorted(expected)


@pytest.mark.asyncio
async def test_vector_contribution_is_deterministic(
    seeded_session, searcher, session_factory
) -> None:
    """The record stored with the identical query embedding must rank first on vector.

    Both records pass the structured filters; the vector-strong one stores the
    exact query embedding (cosine distance 0) while the vector-weak one stores
    its negation (distance 2), so vector recall must rank the strong one ahead.
    """
    result = await searcher.search(QUERY_TEXT, _fixed_filter())
    by_id = {c.notice_version_id: c for c in result.candidates}

    async with session_factory() as session:
        strong = await _id_for(session, "text-weak-vector-strong")
        weak = await _id_for(session, "text-strong-vector-weak")

    assert strong in by_id, "vector-strong record must be retrieved"
    assert weak in by_id, "vector-weak record must be retrieved"
    assert by_id[strong].vector_rank is not None
    assert by_id[weak].vector_rank is not None
    assert by_id[strong].vector_rank < by_id[weak].vector_rank, (
        "identical embedding must outrank the negated one on vector recall"
    )


@pytest.mark.asyncio
async def test_no_duplicate_candidates_after_fusion(
    seeded_session, searcher
) -> None:
    """RRF must dedupe: each notice version appears at most once."""
    result = await searcher.search(QUERY_TEXT, _fixed_filter())
    ids = [c.notice_version_id for c in result.candidates]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_degradation_returns_lexical_without_vector(
    seeded_session, session_factory
) -> None:
    """When embedding fails, return lexical results marked degraded."""

    class FailingProvider:
        dimension = 1024

        async def embed(self, texts: list[str]) -> list[list[float]]:  # noqa: ARG002
            raise RuntimeError("embedding endpoint unavailable")

    failing_searcher = HybridSearcher(
        session_factory=session_factory, provider=FailingProvider(), top_k=10
    )
    result = await failing_searcher.search(QUERY_TEXT, _fixed_filter())

    assert result.degraded_modes == ["vector_unavailable"]
    assert result.candidates, "lexical results must still be returned"
    for candidate in result.candidates:
        assert candidate.vector_rank is None
        assert candidate.lexical_rank is not None  # pure lexical ranking remains


# ---------------------------------------------------------------------------
# Helpers to map external_id → notice_version_id deterministically.


async def _id_for(session: AsyncSession, external_id: str) -> str:
    statement = sa.select(NoticeVersion.id).join(SourceNotice).where(
        SourceNotice.external_id == external_id
    )
    result = await session.execute(statement)
    return str(result.scalar_one())


async def _expected_passing_ids(session: AsyncSession) -> list[str]:
    return [
        await _id_for(session, "sichuan-strong"),
        await _id_for(session, "chongqing-strong"),
        await _id_for(session, "text-weak-vector-strong"),
        await _id_for(session, "text-strong-vector-weak"),
    ]


async def _expected_excluded_ids(session: AsyncSession) -> list[str]:
    return [
        await _id_for(session, "region-mismatch"),
        await _id_for(session, "under-budget"),
        await _id_for(session, "expired-date"),
    ]
