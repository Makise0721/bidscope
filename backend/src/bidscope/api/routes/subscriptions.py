"""Subscription API routes for the BidScope API.

Exposes the subscription lifecycle: list, create, pause, and resume. Creation
requires an explicit, confirmed intent (a recurring subscription is never
auto-created). The scheduler process role (``uv run bidscope scheduler``) is
wired separately in the CLI module.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.persistence.models import Subscription
from bidscope.subscriptions.service import SubscriptionService

router = APIRouter(
    prefix="/api/subscriptions",
    tags=["subscriptions"],
    dependencies=[Depends(require_admin_token)],
)


def _subscription_service(request: Request) -> SubscriptionService:
    """Build a :class:`SubscriptionService` from the app's session factory."""
    run_service: RunService = request.app.state.run_service
    return SubscriptionService(session_factory=run_service.session_factory)


class CreateSubscriptionBody(BaseModel):
    intent: dict[str, Any]
    cron_expression: str = "0 9 * * 1"
    timezone: str = "Asia/Shanghai"


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
    """Create a confirmed, active subscription."""
    sub = await service.create_subscription(
        intent=body.intent,
        cron_expression=body.cron_expression,
        timezone=body.timezone,
    )
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
        await session.commit()
    return {"id": sub.id, "status": sub.status}
