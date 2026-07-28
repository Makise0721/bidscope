"""Direct security tests for the shared admin-token dependency."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bidscope.api.auth import MAX_ADMIN_TOKEN_HEADER_LENGTH, require_admin_token
from bidscope.api.routes import evaluations, events, inbox, reports, runs, sources, subscriptions
from bidscope.config import Settings
from bidscope.main import create_app
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

ADMIN_TOKEN = "test-admin-token-sentinel-0123456789"
INVALID_TOKEN = "wrong-admin-token-sentinel"
UNICODE_ADMIN_TOKEN = "test-admin-token-é-sentinel-0123456789"


def _production_settings(admin_token: str | None = ADMIN_TOKEN) -> Settings:
    """Return self-contained production settings without touching local services."""
    return Settings(
        app_mode="production",
        admin_token=admin_token,
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope-test",
        s3_access_key="test-access-key-sentinel",
        s3_secret_key="test-secret-key-sentinel",
        allowed_origins=["https://console.example.test"],
        trusted_hosts=["api.example.test"],
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


def _settings(app_mode: str, admin_token: str | None = ADMIN_TOKEN) -> Settings:
    if app_mode == "production":
        return _production_settings(admin_token)
    return Settings(app_mode=app_mode, admin_token=admin_token)


def _production_client() -> TestClient:
    """Create a production client without entering its database-backed lifespan."""
    return TestClient(
        create_app(_production_settings()), base_url="https://api.example.test"
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


def _run_dependency(settings: Settings, token: str | bytes | None = None) -> None:
    asyncio.run(require_admin_token(_request(settings, token)))


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize(
    "provided_token",
    (None, "", INVALID_TOKEN, "x" * (MAX_ADMIN_TOKEN_HEADER_LENGTH + 1)),
)
def test_strict_modes_reject_invalid_admin_tokens_identically(
    app_mode: str, provided_token: str | None
) -> None:
    with pytest.raises(HTTPException) as raised:
        _run_dependency(_settings(app_mode), provided_token)

    assert (raised.value.status_code, raised.value.detail) == (401, "invalid admin token")
    assert ADMIN_TOKEN not in str(raised.value)
    assert provided_token not in str(raised.value) if provided_token else True


@pytest.mark.parametrize("app_mode", ("development", "production"))
def test_strict_modes_accept_the_configured_admin_token(app_mode: str) -> None:
    _run_dependency(_settings(app_mode), ADMIN_TOKEN)


@pytest.mark.parametrize("app_mode", ("development", "production"))
def test_strict_modes_accept_unicode_admin_tokens_from_raw_utf8_headers(
    app_mode: str,
) -> None:
    _run_dependency(
        _settings(app_mode, UNICODE_ADMIN_TOKEN), UNICODE_ADMIN_TOKEN.encode("utf-8")
    )


@pytest.mark.parametrize("app_mode", ("development", "production"))
def test_strict_modes_reject_nonmatching_non_ascii_raw_headers_generically(
    app_mode: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        _run_dependency(_settings(app_mode), b"\xc3\xa9")

    assert (raised.value.status_code, raised.value.detail) == (401, "invalid admin token")
    assert ADMIN_TOKEN not in str(raised.value)


@pytest.mark.parametrize("app_mode", ("demo", "test"))
def test_demo_and_test_modes_bypass_admin_token_without_a_header(app_mode: str) -> None:
    _run_dependency(_settings(app_mode))


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize("configured_token", ("", " "))
def test_strict_blank_configured_tokens_cannot_reach_successful_authentication(
    app_mode: str, configured_token: str
) -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        _settings(app_mode, configured_token)


def test_development_without_a_configured_admin_token_fails_at_configuration() -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        _settings("development", admin_token=None)


def test_strict_dependency_rejects_missing_configured_admin_token() -> None:
    request = _request(_settings("development"), ADMIN_TOKEN)
    request.app.state.settings.admin_token = None

    with pytest.raises(HTTPException) as raised:
        asyncio.run(require_admin_token(request))

    assert (raised.value.status_code, raised.value.detail) == (401, "invalid admin token")
    assert ADMIN_TOKEN not in str(raised.value)


def test_production_health_is_public_but_business_paths_require_admin_token() -> None:
    client = _production_client()
    try:
        health_response = client.get("/healthz")
        assert health_response.status_code == 200
        assert health_response.json() == {"status": "ok", "mode": "production"}

        for path in (
            "/api/runs",
            "/api/runs/00000000-0000-0000-0000-000000000000/events",
            "/api/reports/00000000-0000-0000-0000-000000000000",
            "/api/subscriptions",
            "/api/inbox-events",
            "/api/sources",
            "/api/evaluations",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.json() == {"detail": "invalid admin token"}
    finally:
        client.close()


def test_all_business_source_routers_require_the_admin_token() -> None:
    for router in (
        runs.router,
        events.router,
        reports.router,
        subscriptions.router,
        inbox.router,
        sources.router,
        evaluations.router,
    ):
        assert any(
            dependency.dependency is require_admin_token for dependency in router.dependencies
        )


def test_registered_business_routes_keep_the_shared_admin_dependency() -> None:
    app = create_app(_production_settings())
    protected_prefixes = (
        "/api/runs",
        "/api/reports",
        "/api/subscriptions",
        "/api/inbox-events",
        "/api/sources",
        "/api/evaluations",
    )

    registered_business_routes: list[APIRoute] = []
    for route in app.routes:
        nested_router = getattr(route, "original_router", None)
        candidate_routes = (
            nested_router.routes if nested_router is not None else (route,)
        )
        registered_business_routes.extend(
            candidate
            for candidate in candidate_routes
            if isinstance(candidate, APIRoute)
            and any(
                candidate.path == prefix or candidate.path.startswith(f"{prefix}/")
                for prefix in protected_prefixes
            )
        )

    assert registered_business_routes
    for route in registered_business_routes:
        assert any(
            dependency.call is require_admin_token for dependency in route.dependant.dependencies
        ), route.path


def test_correct_admin_token_reaches_a_business_route() -> None:
    async def get_run(_run_id: str) -> None:
        return None

    app = create_app(_production_settings())
    app.state.run_service = SimpleNamespace(get_run=get_run)
    client = TestClient(app, base_url="https://api.example.test")

    try:
        response = client.get(
            "/api/runs/00000000-0000-0000-0000-000000000000",
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
    finally:
        client.close()

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


def test_production_hides_test_controls_on_an_allowed_host() -> None:
    client = _production_client()
    try:
        response = client.post("/api/test-controls/fail-next-node")
        assert response.status_code == 404
    finally:
        client.close()


def test_production_rejects_invalid_hosts_and_limits_cors_to_configured_origin() -> None:
    client = _production_client()
    try:
        invalid_host_response = client.get("/healthz", headers={"Host": "other.example.test"})
        assert invalid_host_response.status_code == 400

        allowed_preflight = client.options(
            "/api/runs",
            headers={
                "Origin": "https://console.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type, x-admin-token, idempotency-key"
                ),
            },
        )
        assert allowed_preflight.status_code == 200
        assert (
            allowed_preflight.headers["access-control-allow-origin"]
            == "https://console.example.test"
        )
        assert {"x-admin-token", "idempotency-key"} <= {
            header.strip().lower()
            for header in allowed_preflight.headers["access-control-allow-headers"].split(",")
        }
        assert "access-control-allow-credentials" not in allowed_preflight.headers

        disallowed_preflight = client.options(
            "/api/runs",
            headers={
                "Origin": "https://other.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-admin-token, idempotency-key",
            },
        )
        assert disallowed_preflight.status_code == 400
        assert "access-control-allow-origin" not in disallowed_preflight.headers
    finally:
        client.close()
