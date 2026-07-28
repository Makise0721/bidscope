"""Transactional persistence for evidence-backed online reports.

Online report rows are the durable source of truth. DOCX is a derived object
attached later by :class:`bidscope.delivery.docx.ReportDelivery`, so an object
store failure cannot discard a report that has already passed evidence checks.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

import sqlalchemy as sa
from bidscope.delivery.docx import DeliveryError, ExportRecord, ReportDelivery
from bidscope.delivery.objects import ObjectStore
from bidscope.domain.notices import NoticeEvidence as DomainEvidence
from bidscope.domain.reports import (
    Report as DomainReport,
)
from bidscope.domain.reports import (
    ReportCitation as DomainCitation,
)
from bidscope.domain.reports import (
    ReportClaim as DomainClaim,
)
from bidscope.domain.reports import (
    ReportItem as DomainItem,
)
from bidscope.observability import METRICS_REGISTRY
from bidscope.persistence.models import (
    NoticeEvidence,
    ReportCitation,
    ReportClaim,
    ReportClaimCitation,
    ReportItem,
)
from bidscope.persistence.models import Report as ReportModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

EvidenceBinding = DomainEvidence | list[DomainEvidence] | tuple[DomainEvidence, ...]


@dataclass(frozen=True)
class PersistedReport:
    """A typed online report together with its durable relational identity."""

    id: str
    report: DomainReport
    docx_object_key: str | None


class ReportPersistence:
    """Persist online reports and delegate DOCX attachment to a narrow exporter."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self.store = store
        self._delivery = ReportDelivery(store=store, session_factory=session_factory)

    async def persist_online_report(
        self,
        report: DomainReport,
        evidence_by_hash: Mapping[str, EvidenceBinding],
    ) -> PersistedReport:
        """Create one evidence-bound report per run, or load its existing projection.

        A single transaction covers the report, ordered items, claims, and both
        citation relation types. Evidence is never copied from the run state:
        every citation resolves to an immutable ``notice_evidence`` row before
        the transaction can commit.
        """
        start = time.monotonic()
        try:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        existing = await self._find_by_run_id(session, report.run_id)
                        if existing is not None:
                            return await self._project(session, existing)

                        row = ReportModel(
                            run_id=report.run_id,
                            export_key=f"online:{report.run_id}",
                            conditions=report.query_conditions,
                            freshness_window=report.freshness_window,
                            source_availability=report.source_availability,
                            completeness_warning=report.completeness_warning,
                            generated_at=report.generated_at,
                        )
                        session.add(row)
                        await session.flush()

                        for rank, item in enumerate(report.items):
                            persisted_item = ReportItem(
                                report_id=row.id,
                                rank=rank,
                                notice_version_id=item.notice_id,
                                title=item.title,
                                known_fields=item.known_fields,
                                unknown_fields=item.unknown_fields,
                                relevance_reason=item.relevance_reason,
                                risk_note=item.risk_note,
                            )
                            session.add(persisted_item)
                            await session.flush()

                            evidence_ids = await self._persist_item_citations(
                                session, persisted_item, item, evidence_by_hash
                            )
                            await self._persist_claims(
                                session, persisted_item, item, evidence_ids, evidence_by_hash
                            )

                    return PersistedReport(
                        id=str(row.id), report=report, docx_object_key=row.docx_object_key
                    )
                except IntegrityError:
                    existing = await self._find_by_run_id(session, report.run_id)
                    if existing is None:
                        raise
                    return await self._project(session, existing)
        finally:
            try:
                METRICS_REGISTRY.observe(
                    "bidscope_report_delivery_duration_seconds",
                    max(time.monotonic() - start, 0.0),
                    {"format": "json"},
                )
            except Exception:
                logger.warning("metrics_json_delivery_failed", exc_info=True)

    async def load_online_report(self, run_id: str) -> PersistedReport | None:
        """Hydrate the complete persisted report for a delivery-only retry."""
        async with self._session_factory() as session:
            row = await self._find_by_run_id(session, run_id)
            return None if row is None else await self._project(session, row)

    async def export_docx(self, persisted: PersistedReport) -> ExportRecord:
        """Render and attach a DOCX to an already durable online report."""
        start = time.monotonic()
        try:
            return await self._delivery.export_report(persisted)
        finally:
            try:
                METRICS_REGISTRY.observe(
                    "bidscope_report_delivery_duration_seconds",
                    max(time.monotonic() - start, 0.0),
                    {"format": "docx"},
                )
            except Exception:
                logger.warning("metrics_docx_delivery_failed", exc_info=True)

    async def _persist_item_citations(
        self,
        session: AsyncSession,
        persisted_item: ReportItem,
        item: DomainItem,
        evidence_by_hash: Mapping[str, EvidenceBinding],
    ) -> dict[str, str]:
        evidence_ids: dict[str, str] = {}
        for ordinal, citation in enumerate(item.citations):
            evidence = await self._resolve_evidence(
                session, item.notice_id, citation.evidence_id, evidence_by_hash
            )
            evidence_ids[citation.evidence_id] = str(evidence.id)
            session.add(ReportCitation(
                report_item_id=persisted_item.id,
                ordinal=ordinal,
                evidence_id=evidence.id,
                label=citation.label,
                span_start=evidence.start,
                span_end=evidence.end,
            ))
        return evidence_ids

    async def _persist_claims(
        self,
        session: AsyncSession,
        persisted_item: ReportItem,
        item: DomainItem,
        evidence_ids: dict[str, str],
        evidence_by_hash: Mapping[str, EvidenceBinding],
    ) -> None:
        for ordinal, claim in enumerate(item.claims):
            persisted_claim = ReportClaim(
                report_item_id=persisted_item.id,
                ordinal=ordinal,
                text=claim.text,
            )
            session.add(persisted_claim)
            await session.flush()
            seen_citation_ids: set[str] = set()
            citation_ordinal = 0
            for evidence_hash in claim.citation_ids:
                if evidence_hash in seen_citation_ids:
                    continue
                seen_citation_ids.add(evidence_hash)
                evidence_id = evidence_ids.get(evidence_hash)
                if evidence_id is None:
                    evidence = await self._resolve_evidence(
                        session, item.notice_id, evidence_hash, evidence_by_hash
                    )
                    evidence_id = str(evidence.id)
                    evidence_ids[evidence_hash] = evidence_id
                session.add(ReportClaimCitation(
                    report_claim_id=persisted_claim.id,
                    ordinal=citation_ordinal,
                    evidence_id=evidence_id,
                    label=self._citation_label(item, evidence_hash),
                ))
                citation_ordinal += 1

    @staticmethod
    def _citation_label(item: DomainItem, evidence_hash: str) -> str | None:
        for citation in item.citations:
            if citation.evidence_id == evidence_hash:
                return citation.label
        return None

    async def _resolve_evidence(
        self,
        session: AsyncSession,
        notice_version_id: str,
        evidence_hash: str,
        evidence_by_hash: Mapping[str, EvidenceBinding],
    ) -> NoticeEvidence:
        binding = evidence_by_hash.get(evidence_hash)
        candidates = binding if isinstance(binding, (list, tuple)) else (binding,)
        supplied = next(
            (
                evidence
                for evidence in candidates
                if evidence is not None
                and str(evidence.notice_version_id) == str(notice_version_id)
            ),
            None,
        )
        if supplied is None:
            raise DeliveryError("report citation is not bound to this notice version")

        result = await session.execute(
            sa.select(NoticeEvidence)
            .where(
                NoticeEvidence.notice_version_id == notice_version_id,
                NoticeEvidence.span_hash == evidence_hash,
            )
            .order_by(NoticeEvidence.id)
            .limit(1)
        )
        persisted = result.scalar_one_or_none()
        if persisted is None:
            raise DeliveryError("report citation evidence is not persisted")
        return persisted

    async def _find_by_run_id(self, session: AsyncSession, run_id: str) -> ReportModel | None:
        result = await session.execute(
            sa.select(ReportModel).where(ReportModel.run_id == run_id)
        )
        return result.scalar_one_or_none()

    async def _project(self, session: AsyncSession, row: ReportModel) -> PersistedReport:
        item_rows = list((await session.scalars(
            sa.select(ReportItem)
            .where(ReportItem.report_id == row.id)
            .order_by(ReportItem.rank, ReportItem.id)
        )).all())
        item_ids = [item.id for item in item_rows]
        citations_by_item: dict[str, list[DomainCitation]] = defaultdict(list)
        claims_by_item: dict[str, list[DomainClaim]] = defaultdict(list)

        if item_ids:
            citation_rows = await session.execute(
                sa.select(ReportCitation, NoticeEvidence.span_hash)
                .join(NoticeEvidence, ReportCitation.evidence_id == NoticeEvidence.id)
                .where(ReportCitation.report_item_id.in_(item_ids))
                .order_by(ReportCitation.report_item_id, ReportCitation.ordinal, ReportCitation.id)
            )
            for citation, span_hash in citation_rows:
                citations_by_item[str(citation.report_item_id)].append(DomainCitation(
                    evidence_id=span_hash, label=citation.label
                ))

            claim_rows = list((await session.scalars(
                sa.select(ReportClaim)
                .where(ReportClaim.report_item_id.in_(item_ids))
                .order_by(ReportClaim.report_item_id, ReportClaim.ordinal, ReportClaim.id)
            )).all())
            claim_ids = [claim.id for claim in claim_rows]
            citation_ids_by_claim: dict[str, list[str]] = defaultdict(list)
            if claim_ids:
                claim_citations = await session.execute(
                    sa.select(ReportClaimCitation.report_claim_id, NoticeEvidence.span_hash)
                    .join(NoticeEvidence, ReportClaimCitation.evidence_id == NoticeEvidence.id)
                    .where(ReportClaimCitation.report_claim_id.in_(claim_ids))
                    .order_by(
                        ReportClaimCitation.report_claim_id,
                        ReportClaimCitation.ordinal,
                        ReportClaimCitation.id,
                    )
                )
                for claim_id, span_hash in claim_citations:
                    citation_ids_by_claim[str(claim_id)].append(span_hash)
            for claim in claim_rows:
                claim_ids = citation_ids_by_claim[str(claim.id)]
                if claim_ids:
                    claims_by_item[str(claim.report_item_id)].append(DomainClaim(
                        text=claim.text, citation_ids=claim_ids
                    ))

        domain_items = [
            DomainItem(
                notice_id=str(item.notice_version_id),
                title=item.title,
                known_fields={str(key): str(value) for key, value in item.known_fields.items()},
                unknown_fields=list(item.unknown_fields),
                relevance_reason=item.relevance_reason,
                risk_note=item.risk_note,
                citations=citations_by_item[str(item.id)],
                claims=claims_by_item[str(item.id)],
            )
            for item in item_rows
        ]
        domain_report = DomainReport(
            run_id=str(row.run_id),
            generated_at=row.generated_at,
            query_conditions={str(key): str(value) for key, value in row.conditions.items()},
            freshness_window=row.freshness_window,
            source_availability=list(row.source_availability or []),
            completeness_warning=row.completeness_warning,
            items=domain_items,
        )
        return PersistedReport(
            id=str(row.id), report=domain_report, docx_object_key=row.docx_object_key
        )


__all__ = ["PersistedReport", "ReportPersistence"]
