"""Authentication dependencies for public API routes."""

from __future__ import annotations

from secrets import compare_digest
from typing import Any

from fastapi import HTTPException, Request

from bidscope.config import MAX_ADMIN_TOKEN_HEADER_LENGTH


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
    if not expected or not provided:
        raise HTTPException(status_code=401, detail="invalid admin token")
    try:
        expected_bytes = expected.encode("utf-8")
        provided_bytes = provided.encode("latin-1")
    except UnicodeEncodeError:
        raise HTTPException(status_code=401, detail="invalid admin token") from None
    if len(expected_bytes) > MAX_ADMIN_TOKEN_HEADER_LENGTH:
        raise HTTPException(status_code=401, detail="invalid admin token")
    if len(provided_bytes) > MAX_ADMIN_TOKEN_HEADER_LENGTH:
        raise HTTPException(status_code=401, detail="invalid admin token")
    if not compare_digest(provided_bytes, expected_bytes):
        raise HTTPException(status_code=401, detail="invalid admin token")
