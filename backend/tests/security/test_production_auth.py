"""Direct security tests for the shared admin-token dependency."""

from __future__ import annotations

import pytest
from bidscope.api.auth import MAX_ADMIN_TOKEN_HEADER_LENGTH, require_admin_token
from bidscope.config import Settings
from bidscope.main import create_app
from fastapi import HTTPException, Request
from pydantic import ValidationError

ADMIN_TOKEN = "test-admin-token-sentinel-0123456789"
INVALID_TOKEN = "wrong-admin-token-sentinel"
UNICODE_ADMIN_TOKEN = "test-admin-token-é-sentinel-0123456789"


def _settings(app_mode: str, admin_token: str | None = ADMIN_TOKEN) -> Settings:
    if app_mode != "production":
        return Settings(app_mode=app_mode, admin_token=admin_token)

    return Settings(
        app_mode="production",
        admin_token=admin_token,
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope-test",
        s3_access_key="test-access-key-sentinel",
        s3_secret_key="test-secret-key-sentinel",
        allowed_origins=["https://bidscope.example.test"],
        trusted_hosts=["bidscope.example.test"],
        external_scheme="https",
        database_url=(
            "postgresql+asyncpg://bidscope:database-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
        checkpoint_database_url=(
            "postgresql+psycopg://bidscope:checkpoint-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
    )


def _request(settings: Settings, token: str | bytes | None = None) -> Request:
    headers = (
        []
        if token is None
        else [(b"x-admin-token", token if isinstance(token, bytes) else token.encode())]
    )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "app": create_app(settings),
        }
    )


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize(
    "provided_token",
    (None, "", INVALID_TOKEN, "x" * (MAX_ADMIN_TOKEN_HEADER_LENGTH + 1)),
)
async def test_strict_modes_reject_invalid_admin_tokens_identically(
    app_mode: str, provided_token: str | None
) -> None:
    request = _request(_settings(app_mode), provided_token)

    with pytest.raises(HTTPException) as raised:
        await require_admin_token(request)

    assert (raised.value.status_code, raised.value.detail) == (401, "invalid admin token")
    assert ADMIN_TOKEN not in str(raised.value)
    assert provided_token not in str(raised.value) if provided_token else True


@pytest.mark.parametrize("app_mode", ("development", "production"))
async def test_strict_modes_accept_the_configured_admin_token(app_mode: str) -> None:
    await require_admin_token(_request(_settings(app_mode), ADMIN_TOKEN))


@pytest.mark.parametrize("app_mode", ("development", "production"))
async def test_strict_modes_accept_unicode_admin_tokens_from_raw_utf8_headers(
    app_mode: str,
) -> None:
    await require_admin_token(
        _request(_settings(app_mode, UNICODE_ADMIN_TOKEN), UNICODE_ADMIN_TOKEN.encode("utf-8"))
    )


@pytest.mark.parametrize("app_mode", ("development", "production"))
async def test_strict_modes_reject_nonmatching_non_ascii_raw_headers_generically(
    app_mode: str,
) -> None:
    request = _request(_settings(app_mode), b"\xc3\xa9")

    with pytest.raises(HTTPException) as raised:
        await require_admin_token(request)

    assert (raised.value.status_code, raised.value.detail) == (401, "invalid admin token")
    assert ADMIN_TOKEN not in str(raised.value)


@pytest.mark.parametrize("app_mode", ("demo", "test"))
async def test_demo_and_test_modes_bypass_admin_token_without_a_header(app_mode: str) -> None:
    await require_admin_token(_request(_settings(app_mode)))


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize("configured_token", ("", " "))
async def test_strict_blank_configured_tokens_cannot_reach_successful_authentication(
    app_mode: str, configured_token: str
) -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        _settings(app_mode, configured_token)


async def test_development_without_a_configured_admin_token_fails_at_configuration() -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        _settings("development", admin_token=None)
