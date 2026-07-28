"""Subscription API routes for the BidScope API.

Exposes the subscription lifecycle: list, create, pause, and resume. Creation
is *derived*: a caller posts a completed, confirmed run id and the service
materializes a subscription from that run's normalized search intent. The
cron expression and timezone come from the run's ``schedule`` and are never
overridden by the request body. The scheduler process role
(``uv run bidscope scheduler``) is wired separately in the CLI module.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.delivery.reports import ReportPersistence
from bidscope.persistence.models import Subscription
from bidscope.subscriptions.service import (
    SubscriptionCreateError,
    SubscriptionIntentError,
    SubscriptionService,
)

router = APIRouter(
    prefix="/api/subscriptions",
    tags=["subscriptions"],
    dependencies=[Depends(require_admin_token)],
)


def _subscription_service(request: Request) -> SubscriptionService:
    """Build a :class:`SubscriptionService` over the shared run service.

    The service injects the API's :class:`RunService` (for real graph execution
    on each tick) and a :class:`ReportPersistence` gate (so the API and the
    scheduler share the same online-report truth).
    """
    run_service: RunService = request.app.state.run_service
    report_persistence = ReportPersistence(
        run_service.session_factory, run_service.object_store,
    )
    return SubscriptionService(
        session_factory=run_service.session_factory,
        run_service=run_service,
        report_persistence=report_persistence,
    )


class CreateSubscriptionBody(BaseModel):
    run_id: str


@router.get("")
async def list_subscriptions(
    service: SubscriptionService = Depends(_subscription_service),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List a bounded, deterministic set of subscriptions."""
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(Subscription)
            .order_by(Subscription.created_at.desc(), Subscription.id)
            .limit(limit)
        )
        rows = result.scalars()
    return [
        {
            "id": row.id,
            "status": row.status,
            "cron_expression": row.cron_expression,
            "next_run_at": (row.normalized_intent or {}).get("__next_run_at"),
            "last_successful_run_at": row.last_successful_run_at.isoformat()
            if row.last_successful_run_at
            else None,
        }
        for row in rows
    ]


@router.post("", status_code=201)
async def create_subscription(
    body: CreateSubscriptionBody,
    service: SubscriptionService = Depends(_subscription_service),
) -> dict[str, Any]:
    """Create an active subscription from a completed, confirmed run."""
    try:
        sub = await service.create_from_run(body.run_id)
    except SubscriptionCreateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SubscriptionIntentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"id": sub.id, "status": sub.status, "cron_expression": sub.cron_expression}


@router.post("/{subscription_id}/pause")
async def pause_subscription(
    subscription_id: str,
    service: SubscriptionService = Depends(_subscription_service),
) -> dict[str, Any]:
    """Pause an active subscription."""
    async with service.session_factory() as session:
        sub = await session.get(Subscription, subscription_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        if sub.status != "active":
            raise HTTPException(status_code=409, detail="subscription is not active")
        sub.status = "paused"
        await record_audit_event(
            session,
            AuditContext(
                method="POST",
                path=f"/api/subscriptions/{subscription_id}/pause",
                subscription_id=str(sub.id),
            ),
            AuditEventType.SUBSCRIPTION_PAUSED,
            AuditOutcome.SUCCESS,
            {"status": sub.status},
        )
        await session.commit()
    return {"id": sub.id, "status": sub.status}


@router.post("/{subscription_id}/resume")
async def resume_subscription(
    subscription_id: str,
    service: SubscriptionService = Depends(_subscription_service),
) -> dict[str, Any]:
    """Resume a paused subscription."""
    async with service.session_factory() as session:
        sub = await session.get(Subscription, subscription_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="subscription not found")
        if sub.status != "paused":
            raise HTTPException(status_code=409, detail="subscription is not paused")
        sub.status = "active"
        await record_audit_event(
            session,
            AuditContext(
                method="POST",
                path=f"/api/subscriptions/{subscription_id}/resume",
                subscription_id=str(sub.id),
            ),
            AuditEventType.SUBSCRIPTION_RESUMED,
            AuditOutcome.SUCCESS,
            {"status": sub.status},
        )
        await session.commit()
    return {"id": sub.id, "status": sub.status}
