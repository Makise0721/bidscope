"""Bounded evaluation result read APIs for operational views."""

from __future__ import annotations

from numbers import Real
from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from bidscope.api.dependencies import RunService
from bidscope.persistence.models import EvalRun

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


def _run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.run_service)


def _metric_value(value: Any) -> dict[str, float | None]:
    """Normalize persisted metrics without inventing missing measurements."""
    if isinstance(value, dict):
        measured = value.get("measured")
        target = value.get("target")
        return {
            "measured": float(measured) if isinstance(measured, Real) else None,
            "target": float(target) if isinstance(target, Real) else None,
        }
    return {
        "measured": float(value) if isinstance(value, Real) else None,
        "target": None,
    }


def _pricing_date(pricing_snapshot: dict[str, Any] | None) -> str | None:
    if not isinstance(pricing_snapshot, dict):
        return None
    for key in ("pricing_snapshot_date", "date"):
        value = pricing_snapshot.get(key)
        if isinstance(value, str):
            return value
    return None


def _evaluation_row(row: EvalRun) -> dict[str, Any]:
    metrics = row.metrics if isinstance(row.metrics, dict) else {}
    return {
        "id": row.id,
        "dataset_version": row.dataset_version,
        "model": row.model,
        "status": row.status,
        "environment": row.environment,
        "pricing_snapshot_date": _pricing_date(row.pricing_snapshot),
        "metrics": {
            str(name): _metric_value(value) for name, value in sorted(metrics.items())
        },
    }


@router.get("")
async def list_evaluations(
    service: RunService = Depends(_run_service),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    """List persisted evaluation summaries without prompts or case payloads."""
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(EvalRun)
            .order_by(EvalRun.finished_at.desc().nullslast(), EvalRun.id)
            .limit(limit)
        )
        rows = list(result.scalars())
    return {"items": [_evaluation_row(row) for row in rows]}
