"""Bounded inbox read API for subscription-generated operational events."""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.persistence.models import InboxEvent

router = APIRouter(
    tags=["inbox"], dependencies=[Depends(require_admin_token)]
)


def _message_preview(value: str, max_length: int = 240) -> str:
    """Keep inbox responses useful without returning unbounded messages."""
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


def _run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.run_service)


@router.get("/api/inbox-events")
async def list_inbox_events(
    service: RunService = Depends(_run_service),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    """List only the title/message and read state needed by the inbox UI."""
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(InboxEvent)
            .order_by(InboxEvent.created_at.desc(), InboxEvent.id)
            .limit(limit)
        )
        rows = list(result.scalars())
    return {
        "items": [
            {
                "id": row.id,
                "event_type": row.event_type,
                "title": row.title,
                "message": _message_preview(row.message),
                "read": row.read,
            }
            for row in rows
        ]
    }
