"""Production admin authentication and run idempotency integration tests."""

from __future__ import annotations

from typing import Any

from bidscope.api.dependencies import RunService
from fastapi.testclient import TestClient

RUN_BODY = {"user_request": "查询四川省最近的服务器招标信息。"}


def test_production_post_runs_requires_admin_token(production_client: TestClient) -> None:
    """Production run creation rejects missing and incorrect admin headers."""
    missing = production_client.post("/api/runs", json=RUN_BODY)
    assert missing.status_code == 401

    mismatch = production_client.post(
        "/api/runs", json=RUN_BODY, headers={"X-Admin-Token": "wrong-token"}
    )
    assert mismatch.status_code == 401


def test_production_post_runs_accepts_exact_admin_token(
    production_client: TestClient,
) -> None:
    """The configured production admin token authorizes run creation."""
    response = production_client.post(
        "/api/runs",
        json=RUN_BODY,
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["id"]


def test_run_idempotency_replays_without_duplicate_execution(
    demo_client: TestClient,
    monkeypatch: Any,
) -> None:
    """A repeated key returns the original run and schedules execution once."""
    executions: list[str] = []

    async def fake_execute_run(
        self: RunService, run_id: str, input_data: Any
    ) -> dict[str, Any]:
        executions.append(run_id)
        return {"status": "pending"}

    monkeypatch.setattr(RunService, "execute_run", fake_execute_run)
    headers = {"Idempotency-Key": "api-replay-key"}

    first = demo_client.post("/api/runs", json=RUN_BODY, headers=headers)
    replay = demo_client.post("/api/runs", json=RUN_BODY, headers=headers)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert executions == [first.json()["id"]]


def test_run_idempotency_key_must_not_be_blank(demo_client: TestClient) -> None:
    """An explicitly supplied blank key is invalid rather than auto-generated."""
    response = demo_client.post(
        "/api/runs", json=RUN_BODY, headers={"Idempotency-Key": "  "}
    )
    assert response.status_code == 422
