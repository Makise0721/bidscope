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
from bidscope.config import Settings
from bidscope.main import create_app
from fastapi.testclient import TestClient

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"


def _settings(*, mode: str, tmp_path: Path) -> Settings:
    """Build settings pointing at the test database with an isolated object root."""
    return Settings(
        app_mode=mode,
        database_url=TEST_DB_URL,
        checkpoint_database_url=TEST_CHECKPOINT_URL,
        real_model_enabled=False,
        admin_token="test-admin-token",
        object_store_root=str(tmp_path / "objects"),
        test_control_token="test-controls-token",
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
