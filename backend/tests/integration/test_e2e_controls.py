"""E2E-controls registration tests.

Proves that the ``/api/test-controls/*`` routes are registered *only* when
``app_mode == "test"``. In demo and production mode these endpoints must return
404; in test mode they respond when the correct ``X-Test-Control-Token`` is
provided.
"""

from __future__ import annotations

from typing import cast

import pytest
from bidscope.config import Settings
from bidscope.main import create_app
from fastapi.testclient import TestClient

# Pull in the shared demo/test/production settings fixtures from the API
# integration conftest so this file's tests stay consistent with the rest of
# the suite. ``testpaths`` puts ``backend/tests`` on ``sys.path``, so the
# module is importable as ``integration.api.conftest``.
pytest_plugins = ["integration.api.conftest"]


@pytest.fixture()
def demo_client(demo_settings: object) -> TestClient:
    """A synchronous ``TestClient`` wrapping a demo-mode app."""
    settings = cast(Settings, demo_settings)
    return TestClient(create_app(settings=settings))


@pytest.fixture()
def production_client(production_settings: object) -> TestClient:
    """A synchronous ``TestClient`` wrapping a production-mode app."""
    settings = cast(Settings, production_settings)
    return TestClient(create_app(settings=settings))


@pytest.fixture()
def test_client(test_settings: object) -> TestClient:
    """A synchronous ``TestClient`` wrapping a test-mode app."""
    settings = cast(Settings, test_settings)
    return TestClient(create_app(settings=settings))


def test_control_routes_not_registered_in_demo_mode(demo_client: TestClient) -> None:
    """In demo mode the test-controls routes are not registered → 404."""
    response = demo_client.post(
        "/api/test-controls/fail-next-node",
        headers={"X-Test-Control-Token": "test-controls-token"},
    )
    assert response.status_code == 404


def test_import_batch_2_not_registered_in_demo_mode(demo_client: TestClient) -> None:
    """In demo mode the import-batch-2 route is not registered → 404."""
    response = demo_client.post(
        "/api/test-controls/import-batch-2",
        headers={"X-Test-Control-Token": "test-controls-token"},
    )
    assert response.status_code == 404


def test_control_routes_not_registered_in_production_mode(
    production_client: TestClient,
) -> None:
    """In production mode the test-controls routes are not registered → 404."""
    response = production_client.post(
        "/api/test-controls/fail-next-node",
        headers={"X-Test-Control-Token": "test-controls-token"},
    )
    assert response.status_code == 404


def test_control_routes_registered_in_test_mode(test_client: TestClient) -> None:
    """In test mode the test-controls routes respond 200 with a valid token."""
    response = test_client.post(
        "/api/test-controls/fail-next-node",
        headers={"X-Test-Control-Token": "test-controls-token"},
        json={"node": "parse_intent"},
    )
    assert response.status_code == 200
    assert response.json() == {"fail_next_node": True}
