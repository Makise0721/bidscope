"""Authentication dependencies for public API routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


async def require_admin_token(request: Request) -> None:
    """Require the configured admin token outside demo and test modes."""
    settings: Any = request.app.state.settings
    if settings.app_mode in {"demo", "test"}:
        return

    expected = settings.admin_token
    provided = request.headers.get("X-Admin-Token")
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")
