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

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunCapacityError, RunQueryResult, RunService
from bidscope.audit import AuditContext
from bidscope.persistence.models import QueryRun

router = APIRouter(
    prefix="/api/runs", tags=["runs"], dependencies=[Depends(require_admin_token)]
)


def get_run_service(request: Request) -> RunService:
    """Resolve the shared :class:`RunService` from app state."""
    from typing import cast

    return cast(RunService, request.app.state.run_service)


class CreateRunBody(BaseModel):
    user_request: str = Field(..., min_length=1, max_length=4000)


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
    response: Response,
    request: Request,
    service: RunService = Depends(get_run_service),
) -> dict[str, Any]:
    """Create or replay a run and schedule new work exactly once."""
    user_request = body.user_request.strip()
    if not user_request:
        raise HTTPException(status_code=422, detail="user_request must not be empty")

    supplied_key = request.headers.get("Idempotency-Key")
    if supplied_key is not None and not supplied_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    run_key = supplied_key.strip() if supplied_key is not None else str(uuid.uuid4())

    run_id, created = await service.create_run(
        user_request,
        run_key=run_key,
        audit_context=AuditContext(
            method="POST",
            path="/api/runs",
            run_id=None,
        ),
    )
    run = await service.get_run(run_id)
    if not created and run is not None and run.user_request != user_request:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key is already bound to a different user_request",
        )
    if created:
        # Schedule only after the pending row commits, so acknowledged work is durable.
        try:
            service.schedule_run(run_id, {"user_request": user_request})
        except RunCapacityError as error:
            await service._update_status(
                run_id,
                "retryable",
                error={"code": error.code, "message": "run capacity exhausted", "details": {}},
                expected_status="pending",
            )
            response.status_code = 429
            response.headers["Retry-After"] = "5"
            raise HTTPException(status_code=429, detail=error.code) from error
    else:
        response.status_code = 200
    return RunQueryResult.from_row(run).__dict__ if run else {
        "id": run_id, "status": "pending", "user_request": user_request
    }


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
    except RunCapacityError as error:
        raise HTTPException(
            status_code=429,
            detail=error.code,
            headers={"Retry-After": "5"},
        ) from error
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
    except RunCapacityError as error:
        raise HTTPException(
            status_code=429,
            detail=error.code,
            headers={"Retry-After": "5"},
        ) from error
    except Exception as error:  # noqa: BLE001 - translate service errors to HTTP
        status = getattr(error, "status_code", 409)
        raise HTTPException(status_code=status, detail=str(error)) from error
    return {"id": run_id, "status": result.get("status")}
