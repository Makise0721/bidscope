"""Report routes for the BidScope API.

``GET /api/reports/{id}`` returns the structured report JSON for a run.
``GET /api/reports/{id}/docx`` streams the generated DOCX bytes.

Reports are persisted to the relational ``reports`` table by the delivery layer
when a run completes; these routes read them back.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from bidscope.api.dependencies import RunService
from bidscope.persistence.models import Report

router = APIRouter(prefix="/api/reports", tags=["reports"])


def get_run_service(request: Request) -> RunService:
    from typing import cast

    return cast(RunService, request.app.state.run_service)


def _serialize_report(report: Report) -> dict[str, Any]:
    """Convert a Report row to the API JSON shape."""
    return {
        "id": report.id,
        "run_id": report.run_id,
        "export_key": report.export_key,
        "conditions": report.conditions,
        "freshness_window": report.freshness_window,
        "completeness_warning": report.completeness_warning,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }


async def _fetch_report(service: RunService, run_id: str) -> Report:
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(Report).where(Report.run_id == run_id)
        )
        report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report


@router.get("/{run_id}")
async def get_report(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Return the report JSON for a run."""
    report = await _fetch_report(service, run_id)
    return _serialize_report(report)


@router.get("/{run_id}/docx")
async def download_docx(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> Response:
    """Download the DOCX for a run as a streamed binary response."""
    report = await _fetch_report(service, run_id)
    if not report.docx_object_key:
        raise HTTPException(status_code=404, detail="no DOCX available for this run")
    data = service.object_store.get_bytes(report.docx_object_key)
    filename = f"bidscope-{run_id}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
