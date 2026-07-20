"""Tests for the test-controls routes' registration and token guard.

The ``/api/test-controls/*`` routes are registered *only* when
``app_mode == "test"`` and gated by a separate ``X-Test-Control-Token``. They must
return 404 in every other mode, and 401 when the token is missing or wrong.
"""

from __future__ import annotations

from pathlib import Path

from bidscope.config import Settings
from bidscope.main import create_app
from fastapi.testclient import TestClient

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"


def _client_for_mode(mode: str, tmp_path: Path) -> TestClient:
    settings = Settings(
        app_mode=mode,
        database_url=TEST_DB_URL,
        checkpoint_database_url=TEST_CHECKPOINT_URL,
        real_model_enabled=False,
        admin_token="test-admin-token",
        object_store_root=str(tmp_path / "objects"),
        test_control_token="test-controls-token",
    )
    return TestClient(create_app(settings=settings))


def test_test_controls_404_in_demo_mode(tmp_path: Path) -> None:
    """In demo mode the test-controls routes are not registered → 404."""
    with _client_for_mode("demo", tmp_path) as client:
        response = client.post(
            "/api/test-controls/fail-next-node",
            headers={"X-Test-Control-Token": "test-controls-token"},
        )
        assert response.status_code == 404


def test_test_controls_404_in_production_mode(tmp_path: Path) -> None:
    """In production mode the test-controls routes are not registered → 404."""
    with _client_for_mode("production", tmp_path) as client:
        response = client.post(
            "/api/test-controls/fail-next-node",
            headers={"X-Test-Control-Token": "test-controls-token"},
        )
        assert response.status_code == 404


def test_test_controls_401_without_token(test_settings: Settings, tmp_path: Path) -> None:
    """In test mode, a missing/wrong token is rejected with 401."""
    with TestClient(create_app(settings=test_settings)) as client:
        response = client.post("/api/test-controls/fail-next-node")
        assert response.status_code == 401


def test_test_controls_works_with_token(test_settings: Settings) -> None:
    """In test mode with the correct token the route responds 200."""
    with TestClient(create_app(settings=test_settings)) as client:
        response = client.post(
            "/api/test-controls/fail-next-node",
            headers={"X-Test-Control-Token": "test-controls-token"},
        )
        assert response.status_code == 200
        assert response.json() == {"fail_next_node": True}


def test_test_controls_wrong_token_is_401(test_settings: Settings) -> None:
    """In test mode a wrong token is rejected with 401."""
    with TestClient(create_app(settings=test_settings)) as client:
        response = client.post(
            "/api/test-controls/fail-next-node",
            headers={"X-Test-Control-Token": "wrong-token"},
        )
        assert response.status_code == 401
