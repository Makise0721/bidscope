from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from bidscope.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FailedReadinessProbe:
    async def check(self, **_dependencies: Any) -> dict[str, Any]:
        return {
            "status": "failed",
            "checks": {
                "database": {"status": "failed", "code": "database_unavailable"},
                "checkpoint": {"status": "ok"},
                "object_store": {"status": "ok"},
                "configuration": {"status": "ok"},
            },
        }


@pytest.fixture()
def readiness_client(test_settings: Any) -> AsyncIterator[TestClient]:
    app: FastAPI = create_app(settings=test_settings)
    with TestClient(app) as client:
        yield client


def test_readyz_reports_working_runtime_dependencies(
    readiness_client: TestClient,
) -> None:
    response = readiness_client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": {"status": "ok"},
            "checkpoint": {"status": "ok"},
            "object_store": {"status": "ok"},
            "configuration": {"status": "ok"},
        },
    }


def test_readyz_returns_bounded_503_for_failed_probe(
    test_settings: Any,
) -> None:
    app = create_app(settings=test_settings)
    with TestClient(app) as client:
        client.app.state.readiness_probe = _FailedReadinessProbe()
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "failed",
        "checks": {
            "database": {"status": "failed", "code": "database_unavailable"},
            "checkpoint": {"status": "ok"},
            "object_store": {"status": "ok"},
            "configuration": {"status": "ok"},
        },
    }
    assert len(response.content) < 512
    assert "postgres" not in response.text.lower()
    assert "trace" not in response.text.lower()
