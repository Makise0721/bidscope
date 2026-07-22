"""Run and SSE-event routes for the BidScope API.

``POST /api/runs`` persists a ``pending`` run and schedules the executor task
(``graph/executor.py``'s ``execute``) in the background; the run then progresses
to a terminal or ``awaiting_confirmation`` state without blocking the request.

The state-machine contract:

* ``POST /api/runs/{id}/confirm`` succeeds only when the run is
  ``awaiting_confirmation``; otherwise it returns HTTP 409.
* ``POST /api/runs/{id}/retry`` succeeds only when the run is ``retryable``;
  otherwise HTTP 409.
"""

from __future__ import annotations

import asyncio
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from bidscope.api.dependencies import RunQueryResult, RunService
from bidscope.persistence.models import QueryRun

router = APIRouter(prefix="/api/runs", tags=["runs"])


def get_run_service(request: Request) -> RunService:
    """Resolve the shared :class:`RunService` from app state."""
    from typing import cast

    return cast(RunService, request.app.state.run_service)


class CreateRunBody(BaseModel):
    user_request: str = Field(..., min_length=1)


class ConfirmBody(BaseModel):
    action: str = "approve"


def _request_preview(value: str, max_length: int = 240) -> str:
    """Return a bounded list-view preview without exposing the full request."""
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


@router.post("", status_code=201)
async def create_run(
    body: CreateRunBody,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Create a run (stored as pending) and schedule its execution."""
    user_request = body.user_request.strip()
    if not user_request:
        raise HTTPException(status_code=422, detail="user_request must not be empty")

    run_id = await service.create_run(user_request)
    # Schedule the executor after persisting pending, so a crash between the two
    # never leaves an un-executed run that the API already acknowledged.
    asyncio.create_task(
        service.execute_run(run_id, {"user_request": user_request})
    )
    run = await service.get_run(run_id)
    return RunQueryResult.from_row(run).__dict__ if run else {"id": run_id, "status": "pending"}


@router.get("")
async def list_runs(
    status: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=100),
    service: RunService = Depends(get_run_service),
) -> dict[str, list[dict[str, Any]]]:
    """Return a bounded, deterministic run-history summary."""
    async with service.session_factory() as session:
        statement = (
            sa.select(QueryRun)
            .order_by(QueryRun.created_at.desc(), QueryRun.id)
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(QueryRun.status == status)
        result = await session.execute(statement)
        rows = list(result.scalars())
    return {
        "items": [
            {
                "id": row.id,
                "status": row.status,
                "request_preview": _request_preview(row.user_request),
                "retryable": row.status == "retryable",
            }
            for row in rows
        ]
    }


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Return the current state of a run."""
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunQueryResult.from_row(run).__dict__


@router.post("/{run_id}/confirm")
async def confirm_run(
    run_id: str,
    body: ConfirmBody,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Approve a run awaiting confirmation, resuming it through completion."""
    try:
        result = await service.confirm(run_id)
    except Exception as error:  # noqa: BLE001 - translate service errors to HTTP
        status = getattr(error, "status_code", 409)
        raise HTTPException(status_code=status, detail=str(error)) from error
    return {"id": run_id, "status": result.get("status")}


@router.post("/{run_id}/retry")
async def retry_run(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Retry a retryable run."""
    try:
        result = await service.retry(run_id)
    except Exception as error:  # noqa: BLE001 - translate service errors to HTTP
        status = getattr(error, "status_code", 409)
        raise HTTPException(status_code=status, detail=str(error)) from error
    return {"id": run_id, "status": result.get("status")}
