"""Authentication dependencies for public API routes."""

from __future__ import annotations

from secrets import compare_digest
from typing import Any

from fastapi import HTTPException, Request

MAX_ADMIN_TOKEN_HEADER_LENGTH = 4096


async def require_admin_token(request: Request) -> None:
    """Require the configured admin token outside demo and test modes."""
    settings: Any = request.app.state.settings
    if settings.app_mode in {"demo", "test"}:
        return

    configured_token = settings.admin_token
    expected = (
        configured_token.get_secret_value()
        if configured_token is not None
        else None
    )
    provided = request.headers.get("X-Admin-Token")
    if not expected or not provided or len(provided) > MAX_ADMIN_TOKEN_HEADER_LENGTH:
        raise HTTPException(status_code=401, detail="invalid admin token")
    if not compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")
