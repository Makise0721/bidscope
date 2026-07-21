"""Hybrid tender retrieval: structured filter + trigram + vector + RRF.

The query pipeline follows the order the evidence contract requires:

1. **Structured filtering** — date, region, and budget filters are applied
   first, producing the candidate set. This must happen *before* any top-K
   recall, otherwise an irrelevant region could crowd out a valid result.
2. **Lexical recall** — ``pg_trgm`` title similarity over the candidates.
3. **Vector recall** — pgvector cosine distance over the candidates.
4. **Reciprocal-rank fusion** — the two ranked lists are merged and deduped.

If the embedding provider is unavailable or raises, the vector step is skipped
and the lexical results are returned with ``degraded_modes=["vector_unavailable"]``;
degradation is a valid result, never a system failure.

Retrieval returns notice/version IDs and bounded scoring metadata only — never
the full notice body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from bidscope.persistence.models import NoticeVersion
from bidscope.retrieval.embeddings import RRF_K

#: Equal-weight fusion by default; overridable per searcher instance.
LEXICAL_WEIGHT = 1.0
VECTOR_WEIGHT = 1.0

#: Convenience alias for a SQLAlchemy select statement carrying any columns.
SelectStatement = sa.Select[tuple[Any, ...]]


class EmbeddingProvider(Protocol):
    """The slice of an embedding provider the searcher needs."""

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class RetrievalFilter:
    """Structured filters applied before any recall."""

    regions: list[str] | None = None
    published_from: datetime | None = None
    published_to: datetime | None = None
    min_budget_minor_units: int | None = None


@dataclass
class RetrievalCandidate:
    """A single ranked retrieval result with bounded metadata."""

    notice_version_id: str
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None


@dataclass
class RetrievalResult:
    """The full result of a hybrid retrieval query."""

    query: str
    candidates: list[RetrievalCandidate]
    degraded_modes: list[str]
    filters_applied: dict[str, Any]


def _apply_structured_filters(
    statement: SelectStatement, filters: RetrievalFilter
) -> SelectStatement:
    """Apply structured filters to a notice_versions statement."""
    if filters.regions:
        statement = statement.where(NoticeVersion.region.in_(filters.regions))
    if filters.published_from is not None:
        statement = statement.where(NoticeVersion.publish_date >= filters.published_from)
    if filters.published_to is not None:
        statement = statement.where(NoticeVersion.publish_date <= filters.published_to)
    if filters.min_budget_minor_units is not None:
        statement = statement.where(
            NoticeVersion.budget_minor_units >= filters.min_budget_minor_units
        )
    return statement


def _reciprocal_rank_fuse(
    lexical: list[tuple[str, int]],
    vector: list[tuple[str, int]],
    *,
    rrf_k: int,
    lexical_weight: float,
    vector_weight: float,
) -> list[RetrievalCandidate]:
    """Merge two ``(notice_version_id, rank)`` lists via reciprocal-rank fusion.

    ``rank`` is 1-based. Candidates appearing in both lists have their scores
    summed; the result is sorted descending by score and deduped by ID.
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}

    for ranked, weight, key in (
        (lexical, lexical_weight, "lexical_rank"),
        (vector, vector_weight, "vector_rank"),
    ):
        for index, (notice_version_id, _) in enumerate(ranked):
            rank = index + 1
            scores[notice_version_id] = (
                scores.get(notice_version_id, 0.0) + weight / (rrf_k + rank)
            )
            ranks.setdefault(notice_version_id, {})[key] = rank

    fused = [
        RetrievalCandidate(
            notice_version_id=notice_version_id,
            score=score,
            lexical_rank=ranks[notice_version_id].get("lexical_rank"),
            vector_rank=ranks[notice_version_id].get("vector_rank"),
        )
        for notice_version_id, score in scores.items()
    ]
    fused.sort(key=lambda candidate: candidate.score, reverse=True)
    return fused


class HybridSearcher:
    """Execute the structured → trigram → vector → RRF pipeline."""

    def __init__(
        self,
        session_factory: Any,
        provider: EmbeddingProvider,
        *,
        top_k: int = 20,
        rrf_k: int = RRF_K,
        lexical_weight: float = LEXICAL_WEIGHT,
        vector_weight: float = VECTOR_WEIGHT,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight

    async def search(
        self, query: str, filters: RetrievalFilter | None = None
    ) -> RetrievalResult:
        async with self.session_factory() as session:
            return await self._search(session, query, filters)

    async def _search(
        self, session: AsyncSession, query: str, filters: RetrievalFilter | None = None
    ) -> RetrievalResult:
        filters = filters or RetrievalFilter()
        degraded_modes: list[str] = []

        # Step 1: structured filtering produces the candidate set first. Both
        # recall steps are constrained to these IDs, so an irrelevant region can
        # never crowd out a valid result.
        candidate_statement = _apply_structured_filters(
            sa.select(NoticeVersion.id), filters
        )
        candidate_rows = await session.execute(candidate_statement)
        candidate_ids = [str(row.id) for row in candidate_rows.all()]

        # Step 2: lexical recall over the candidates.
        lexical_statement = (
            sa.select(
                NoticeVersion.id,
                func.similarity(NoticeVersion.title, query).label("similarity"),
            )
            .where(NoticeVersion.id.in_(candidate_ids))
            .order_by(func.similarity(NoticeVersion.title, query).desc())
        )
        lexical = await self._ranks(session, lexical_statement)

        # Step 3: vector recall over the candidates (with degradation).
        vector: list[tuple[str, int]] = []
        try:
            query_embedding = await self._embed_query(query)
        except Exception:  # noqa: BLE001 — any embedding failure triggers degradation
            degraded_modes.append("vector_unavailable")
        else:
            if query_embedding is not None:
                vector_statement = (
                    sa.select(NoticeVersion.id)
                    .where(NoticeVersion.id.in_(candidate_ids))
                    .where(NoticeVersion.embedding.is_not(None))
                    .order_by(NoticeVersion.embedding.cosine_distance(query_embedding))
                )
                vector = await self._ranks(session, vector_statement)

        # Step 4: reciprocal-rank fusion.
        candidates = _reciprocal_rank_fuse(
            lexical,
            vector,
            rrf_k=self.rrf_k,
            lexical_weight=self.lexical_weight,
            vector_weight=self.vector_weight,
        )

        return RetrievalResult(
            query=query,
            candidates=candidates,
            degraded_modes=degraded_modes,
            filters_applied={
                "regions": filters.regions,
                "published_from": filters.published_from,
                "published_to": filters.published_to,
                "min_budget_minor_units": filters.min_budget_minor_units,
            },
        )

    async def _embed_query(self, query: str) -> list[float] | None:
        """Embed the query, returning ``None`` for empty input."""
        if not query.strip():
            return None
        vectors = await self.provider.embed([query])
        return vectors[0] if vectors else None

    async def _ranks(
        self, session: AsyncSession, statement: SelectStatement
    ) -> list[tuple[str, int]]:
        """Execute a ranked statement and return ``(id, 1-based-rank)`` pairs."""
        result = await session.execute(statement.limit(self.top_k))
        return [(str(row.id), index + 1) for index, row in enumerate(result)]
