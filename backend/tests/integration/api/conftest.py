"""Shared fixtures for the API integration tests.

These tests exercise the FastAPI surface end-to-end against the Compose test
database. Each test receives its own synchronous ``TestClient`` wrapping a
freshly created app. Per-test data isolation is provided by the integration
``conftest.py``'s table truncation between tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from bidscope.config import Settings, get_settings
from bidscope.delivery.objects import LocalObjectStore
from bidscope.main import create_app
from fastapi.testclient import TestClient

PRODUCTION_ADMIN_TOKEN = "test-admin-token-012345678901234567890123"


def _settings(*, mode: str, tmp_path: Path) -> Settings:
    """Build settings over the guard-validated test database and isolated storage."""
    guarded_settings = get_settings()
    production_values: dict[str, object] = {}
    if mode == "production":
        production_values = {
            "object_store_type": "s3",
            "s3_endpoint": "http://minio:9000",
            "s3_region": "us-east-1",
            "s3_bucket": "bidscope-test",
            "s3_access_key": "test-access",
            "s3_secret_key": "test-secret",
            "allowed_origins": ["https://bidscope.test"],
            "trusted_hosts": ["bidscope.test"],
            "external_scheme": "https",
        }
    return Settings(
        app_mode=mode,
        database_url=guarded_settings.database_url,
        checkpoint_database_url=guarded_settings.checkpoint_database_url,
        real_model_enabled=False,
        admin_token=(
            PRODUCTION_ADMIN_TOKEN if mode == "production" else "test-admin-token"
        ),
        object_store_root=str(tmp_path / "objects"),
        test_control_token="test-controls-token",
        **production_values,
    )


@pytest.fixture()
def demo_settings(tmp_path: Path) -> Settings:
    """Demo-mode settings: fake model only, no admin token accepted."""
    return _settings(mode="demo", tmp_path=tmp_path)


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    """Test-mode settings: test-controls routes are registered."""
    return _settings(mode="test", tmp_path=tmp_path)


@pytest.fixture()
def production_settings(tmp_path: Path) -> Settings:
    """Production-mode tests-controls routes are NOT registered."""
    return _settings(mode="production", tmp_path=tmp_path)


@pytest.fixture()
def production_client(production_settings: Settings) -> Iterator[TestClient]:
    """Production-mode client for admin-authentication coverage."""
    with TestClient(create_app(settings=production_settings)) as client:
        client.app.state.run_service.object_store = LocalObjectStore(
            production_settings.object_store_root
        )
        yield client


@pytest.fixture()
def development_client(tmp_path: Path) -> Iterator[TestClient]:
    """Development-mode client for admin-authentication coverage."""
    with TestClient(
        create_app(settings=_settings(mode="development", tmp_path=tmp_path))
    ) as client:
        yield client


@pytest.fixture(scope="session")
def demo_client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    """A single synchronous ``TestClient`` wrapping a demo-mode app, per session.

    Session scope means one async engine is created for the whole session and
    disposed once at the end, avoiding per-test engine disposal that crosses
    anyio event loops and breaks subsequent tests.
    """
    tmp_path = tmp_path_factory.mktemp("api-objects")
    app = create_app(settings=_settings(mode="demo", tmp_path=tmp_path))
    with TestClient(app) as client:
        yield client
