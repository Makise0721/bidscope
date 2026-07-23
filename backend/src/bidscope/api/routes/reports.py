"""Report routes for the BidScope API.

``GET /api/reports/{id}`` returns the structured report JSON for a run.
``GET /api/reports/{id}/docx`` streams the generated DOCX bytes.

Reports are persisted to the relational ``reports`` table by the delivery layer
when a run completes; these routes read them back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.persistence.models import NoticeVersion, Report, ReportItem, SourceNotice

router = APIRouter(
    prefix="/api/reports",
    tags=["reports"],
    dependencies=[Depends(require_admin_token)],
)

_REPORT_ITEMS_LIMIT = 100
_ITEM_TEXT_LIMIT = 240
_ITEM_URL_LIMIT = 2048
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


def _serialize_item(item: Any, provenance: Any = None) -> dict[str, Any]:
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
    return serialized


def get_run_service(request: Request) -> RunService:
    from typing import cast

    return cast(RunService, request.app.state.run_service)


def _serialize_report(
    report: Report,
    items: list[Any] | None = None,
    provenance: list[Any] | None = None,
) -> dict[str, Any]:
    """Convert a Report row to a bounded API JSON shape."""
    raw_items = items if items is not None else _safe_attr(report, "items")
    bounded_items = raw_items if isinstance(raw_items, (list, tuple)) else []
    bounded_provenance = provenance or []
    return {
        "id": report.id,
        "run_id": report.run_id,
        "export_key": report.export_key,
        "conditions": report.conditions,
        "freshness_window": report.freshness_window,
        "completeness_warning": report.completeness_warning,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "items": [
            _serialize_item(
                item,
                bounded_provenance[index] if index < len(bounded_provenance) else None,
            )
            for index, item in enumerate(bounded_items[:_REPORT_ITEMS_LIMIT])
        ],
    }


async def _fetch_report(
    service: RunService, run_id: str
) -> tuple[Report, list[ReportItem], list[SourceNotice | None]]:
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(Report).where(Report.run_id == run_id)
        )
        report = result.scalar_one_or_none()
        if report is None:
            raise HTTPException(status_code=404, detail="report not found")
        item_result = await session.execute(
            sa.select(ReportItem, SourceNotice)
            .outerjoin(
                NoticeVersion,
                ReportItem.notice_version_id == NoticeVersion.id,
            )
            .outerjoin(
                SourceNotice,
                NoticeVersion.source_notice_id == SourceNotice.id,
            )
            .where(ReportItem.report_id == report.id)
            .order_by(ReportItem.rank, ReportItem.id)
            .limit(_REPORT_ITEMS_LIMIT)
        )
        rows = list(item_result.all())
    items = [item for item, _ in rows]
    provenance = [source_notice for _, source_notice in rows]
    return report, items, provenance


@router.get("/{run_id}")
async def get_report(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Return the report JSON for a run."""
    report, items, provenance = await _fetch_report(service, run_id)
    return _serialize_report(report, items, provenance)


@router.get("/{run_id}/docx")
async def download_docx(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> Response:
    """Download the DOCX for a run as a streamed binary response."""
    report, _, _ = await _fetch_report(service, run_id)
    if not report.docx_object_key:
        raise HTTPException(status_code=404, detail="no DOCX available for this run")
    data = service.object_store.get_bytes(report.docx_object_key)
    filename = f"bidscope-{run_id}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
