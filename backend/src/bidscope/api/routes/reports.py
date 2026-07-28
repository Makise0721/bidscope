"""Bounded read and download routes for persisted evidence-backed reports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.delivery.docx import DeliveryError
from bidscope.delivery.reports import ReportPersistence
from bidscope.persistence.models import (
    NoticeEvidence,
    NoticeVersion,
    Report,
    ReportCitation,
    ReportClaim,
    ReportClaimCitation,
    ReportItem,
    SourceNotice,
)

router = APIRouter(
    prefix="/api/reports",
    tags=["reports"],
    dependencies=[Depends(require_admin_token)],
)

_REPORT_ITEMS_LIMIT = 100
_ITEM_TEXT_LIMIT = 240
_ITEM_URL_LIMIT = 2048
_EXCERPT_LIMIT = 400
_CLAIMS_LIMIT = 100
_CITATIONS_LIMIT = 100
_BOUNDED_KNOWN_FIELDS = frozenset(
    {
        "budget",
        "budget_currency",
        "deadline",
        "project_number",
        "publish_date",
        "purchaser",
        "region",
        "source",
        "source_url",
        "url",
    }
)


def _safe_attr(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:  # noqa: BLE001 - tolerate unloaded ORM relationships
        return None


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _bounded_known_fields(item: Any) -> dict[str, str]:
    fields = _safe_attr(item, "known_fields")
    if not isinstance(fields, Mapping):
        return {}

    bounded: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or key not in _BOUNDED_KNOWN_FIELDS:
            continue
        limit = _ITEM_URL_LIMIT if key in {"source_url", "url"} else _ITEM_TEXT_LIMIT
        text = _bounded_text(value, limit)
        if text is not None:
            bounded[key] = text
    return bounded


def _item_relationships(item: Any, provenance: Any = None) -> list[Any]:
    relationships = [item]
    if provenance is not None:
        relationships.append(provenance)
    for name in ("source_notice", "notice_version", "notice"):
        related = _safe_attr(item, name)
        if related is not None:
            relationships.append(related)
            source_notice = _safe_attr(related, "source_notice")
            if source_notice is not None:
                relationships.append(source_notice)
    return relationships


def _first_bounded_text(values: list[Any], names: tuple[str, ...], limit: int) -> str | None:
    for value in values:
        for name in names:
            text = _bounded_text(_safe_attr(value, name), limit)
            if text is not None:
                return text
    return None


def _bounded_unknown_fields(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        text
        for item in value[:_ITEM_TEXT_LIMIT]
        if (text := _bounded_text(item, _ITEM_TEXT_LIMIT))
    ]


def _bounded_source_availability(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        text
        for source in value[:_ITEM_TEXT_LIMIT]
        if (text := _bounded_text(source, _ITEM_TEXT_LIMIT))
    ]


def _serialize_citation(citation: Mapping[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {
        "evidence_id": str(citation["evidence_id"]),
        "span_hash": _bounded_text(citation.get("span_hash"), _ITEM_TEXT_LIMIT) or "",
        "start": citation.get("start"),
        "end": citation.get("end"),
        "excerpt": _bounded_text(citation.get("excerpt"), _EXCERPT_LIMIT) or "",
    }
    label = _bounded_text(citation.get("label"), _ITEM_TEXT_LIMIT)
    if label is not None:
        serialized["label"] = label
    return serialized


def _serialize_item(
    item: Any,
    provenance: Any = None,
    citations: Sequence[Mapping[str, Any]] | None = None,
    claims: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    known_fields = _bounded_known_fields(item)
    related = _item_relationships(item, provenance)
    serialized: dict[str, Any] = {
        "title": _bounded_text(_safe_attr(item, "title"), _ITEM_TEXT_LIMIT) or "",
    }

    source = _first_bounded_text(related, ("source",), _ITEM_TEXT_LIMIT)
    if source is None:
        source = known_fields.get("source")
    if source is not None:
        serialized["source"] = source

    url = _first_bounded_text(related, ("url", "source_url", "canonical_url"), _ITEM_URL_LIMIT)
    if url is None:
        url = known_fields.get("url") or known_fields.get("source_url")
    if url is not None:
        serialized["url"] = url

    for name in ("retrieved_at", "hash_prefix", "freshness_days"):
        value = _safe_attr(item, name)
        if isinstance(value, (str, int, float)):
            serialized[name] = str(value)[:_ITEM_TEXT_LIMIT]

    if known_fields:
        serialized["known_fields"] = known_fields
    unknown_fields = _bounded_unknown_fields(_safe_attr(item, "unknown_fields"))
    if unknown_fields:
        serialized["unknown_fields"] = unknown_fields
    relevance_reason = _bounded_text(_safe_attr(item, "relevance_reason"), _ITEM_TEXT_LIMIT)
    if relevance_reason is not None:
        serialized["relevance_reason"] = relevance_reason
    risk_note = _bounded_text(_safe_attr(item, "risk_note"), _ITEM_TEXT_LIMIT)
    if risk_note is not None:
        serialized["risk_note"] = risk_note

    if isinstance(provenance, Mapping):
        bounded_provenance = {
            key: value
            for key, value in {
                "source": _bounded_text(provenance.get("source"), _ITEM_TEXT_LIMIT),
                "source_title": _bounded_text(provenance.get("source_title"), _ITEM_TEXT_LIMIT),
                "source_url": _bounded_text(provenance.get("source_url"), _ITEM_URL_LIMIT),
                "capture_kind": _bounded_text(provenance.get("capture_kind"), _ITEM_TEXT_LIMIT),
                "source_version_id": _bounded_text(
                    provenance.get("source_version_id"), _ITEM_TEXT_LIMIT
                ),
                "parser_version": _bounded_text(provenance.get("parser_version"), _ITEM_TEXT_LIMIT),
            }.items()
            if value is not None
        }
        if bounded_provenance:
            serialized["provenance"] = bounded_provenance

    if citations:
        serialized["citations"] = [
            _serialize_citation(citation) for citation in citations[:_CITATIONS_LIMIT]
        ]
    if claims:
        serialized["claims"] = [
            {
                "text": _bounded_text(claim.get("text"), _ITEM_TEXT_LIMIT) or "",
                "citation_ids": [
                    _bounded_text(value, _ITEM_TEXT_LIMIT) or ""
                    for value in claim.get("citation_ids", [])[:_CITATIONS_LIMIT]
                ],
            }
            for claim in claims[:_CLAIMS_LIMIT]
        ]
    return serialized


def get_run_service(request: Request) -> RunService:
    from typing import cast

    return cast(RunService, request.app.state.run_service)


def _serialize_report(
    report: Report,
    items: list[Any] | None = None,
    provenance: list[Any] | None = None,
    citations: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    claims: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Convert report rows to bounded DTOs without raw snapshot payloads."""
    raw_items = items if items is not None else _safe_attr(report, "items")
    bounded_items = raw_items if isinstance(raw_items, (list, tuple)) else []
    bounded_provenance = provenance or []
    citations = citations or {}
    claims = claims or {}
    return {
        "id": str(report.id),
        "run_id": str(report.run_id),
        "export_key": _bounded_text(report.export_key, _ITEM_TEXT_LIMIT) or "",
        "conditions": {
            str(key)[:_ITEM_TEXT_LIMIT]: str(value)[:_ITEM_TEXT_LIMIT]
            for key, value in (report.conditions or {}).items()
            if isinstance(key, str) and isinstance(value, (str, int, float))
        },
        "freshness_window": _bounded_text(report.freshness_window, _ITEM_TEXT_LIMIT),
        "source_availability": _bounded_source_availability(
            _safe_attr(report, "source_availability")
        ),
        "completeness_warning": _bounded_text(report.completeness_warning, _ITEM_TEXT_LIMIT),
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "items": [
            _serialize_item(
                item,
                bounded_provenance[index] if index < len(bounded_provenance) else None,
                citations.get(str(_safe_attr(item, "id"))),
                claims.get(str(_safe_attr(item, "id"))),
            )
            for index, item in enumerate(bounded_items[:_REPORT_ITEMS_LIMIT])
        ],
    }


async def _record_report_observation(
    service: RunService,
    run_id: str,
    *,
    report_id: str,
    event_type: AuditEventType,
    object_key: str | None = None,
) -> None:
    """Record bounded report observation metadata without blocking the response."""
    try:
        async with service.session_factory() as session:
            await record_audit_event(
                session,
                AuditContext(
                    method="GET" if event_type != AuditEventType.DOCX_RETRIED else "POST",
                    path=f"/api/reports/{run_id}",
                    run_id=run_id,
                    report_id=report_id,
                ),
                event_type,
                AuditOutcome.SUCCESS,
                {"object_key": object_key} if object_key is not None else {},
            )
            await session.commit()
    except Exception:
        # Observation audit must not break an otherwise successful read.
        return


async def _fetch_report(
    service: RunService, run_id: str
) -> tuple[
    Report,
    list[ReportItem],
    list[dict[str, str]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    async with service.session_factory() as session:
        report = await session.scalar(sa.select(Report).where(Report.run_id == run_id))
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        item_result = await session.execute(
            sa.select(ReportItem, NoticeVersion, SourceNotice)
            .join(NoticeVersion, ReportItem.notice_version_id == NoticeVersion.id)
            .join(SourceNotice, NoticeVersion.source_notice_id == SourceNotice.id)
            .where(ReportItem.report_id == report.id)
            .order_by(ReportItem.rank, ReportItem.id)
            .limit(_REPORT_ITEMS_LIMIT)
        )
        rows = list(item_result.all())
        items = [item for item, _, _ in rows]
        item_ids = [item.id for item in items]
        provenance = [
            {
                "source": source.source,
                "source_title": source.title or version.title or item.title,
                "source_url": source.source_url,
                "capture_kind": version.capture_kind,
                "source_version_id": str(version.id),
                "parser_version": version.parser_version,
            }
            for item, version, source in rows
        ]
        citations_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        claims_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if item_ids:
            citation_result = await session.execute(
                sa.select(ReportCitation, NoticeEvidence)
                .join(NoticeEvidence, ReportCitation.evidence_id == NoticeEvidence.id)
                .where(ReportCitation.report_item_id.in_(item_ids))
                .order_by(ReportCitation.report_item_id, ReportCitation.ordinal, ReportCitation.id)
            )
            for citation, evidence in citation_result:
                start = citation.span_start
                end = citation.span_end
                citations_by_item[str(citation.report_item_id)].append({
                    "evidence_id": str(evidence.id),
                    "span_hash": evidence.span_hash,
                    "start": evidence.start if start is None else start,
                    "end": evidence.end if end is None else end,
                    "excerpt": evidence.text,
                    "label": citation.label,
                })

            claim_rows = list((await session.scalars(
                sa.select(ReportClaim)
                .where(ReportClaim.report_item_id.in_(item_ids))
                .order_by(ReportClaim.report_item_id, ReportClaim.ordinal, ReportClaim.id)
            )).all())
            if claim_rows:
                claim_ids = [claim.id for claim in claim_rows]
                citation_ids_by_claim: dict[str, list[str]] = defaultdict(list)
                claim_citation_result = await session.execute(
                    sa.select(ReportClaimCitation.report_claim_id, NoticeEvidence.span_hash)
                    .join(NoticeEvidence, ReportClaimCitation.evidence_id == NoticeEvidence.id)
                    .where(ReportClaimCitation.report_claim_id.in_(claim_ids))
                    .order_by(
                        ReportClaimCitation.report_claim_id,
                        ReportClaimCitation.ordinal,
                        ReportClaimCitation.id,
                    )
                )
                for claim_id, span_hash in claim_citation_result:
                    citation_ids_by_claim[str(claim_id)].append(span_hash)
                for claim in claim_rows:
                    claims_by_item[str(claim.report_item_id)].append({
                        "text": claim.text,
                        "citation_ids": citation_ids_by_claim[str(claim.id)],
                    })
    return report, items, provenance, citations_by_item, claims_by_item


@router.get("/{run_id}")
async def get_report(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Return a bounded evidence-backed online report JSON DTO."""
    report, items, provenance, citations, claims = await _fetch_report(service, run_id)
    await _record_report_observation(
        service,
        run_id,
        report_id=str(report.id),
        event_type=AuditEventType.REPORT_VIEWED,
    )
    return _serialize_report(report, items, provenance, citations, claims)


@router.post("/{run_id}/docx/retry")
async def retry_docx(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, str]:
    """Export a persisted report's DOCX without restarting its run."""
    persistence = ReportPersistence(service.session_factory, service.object_store)
    persisted = await persistence.load_online_report(run_id)
    if persisted is None:
        raise HTTPException(status_code=404, detail="report not found")
    try:
        export = await persistence.export_docx(persisted)
    except DeliveryError as error:
        raise HTTPException(status_code=503, detail="DOCX export failed") from error
    await _record_report_observation(
        service,
        run_id,
        report_id=export.report_id,
        event_type=AuditEventType.DOCX_RETRIED,
        object_key=export.object_key,
    )
    return {
        "report_id": export.report_id,
        "docx_object_key": export.object_key,
    }


@router.get("/{run_id}/docx")
async def download_docx(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> Response:
    """Download an already attached DOCX object for the online report."""
    report, _, _, _, _ = await _fetch_report(service, run_id)
    if not report.docx_object_key:
        raise HTTPException(status_code=404, detail="no DOCX available for this run")
    try:
        data = service.object_store.get_bytes(report.docx_object_key)
    except OSError as error:
        raise HTTPException(status_code=404, detail="DOCX object unavailable") from error
    await _record_report_observation(
        service,
        run_id,
        report_id=str(report.id),
        event_type=AuditEventType.DOCX_VIEWED,
        object_key=report.docx_object_key,
    )
    filename = f"bidscope-{run_id}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
