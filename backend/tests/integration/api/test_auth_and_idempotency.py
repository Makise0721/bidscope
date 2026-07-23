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


def test_development_post_runs_requires_admin_token(
    development_client: TestClient,
) -> None:
    """Development run creation requires the configured admin token."""
    missing = development_client.post("/api/runs", json=RUN_BODY)
    assert missing.status_code == 401

    response = development_client.post(
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
    scheduled: list[str] = []

    def fake_schedule_run(
        self: RunService, run_id: str, input_data: Any
    ) -> object:
        del self, input_data
        scheduled.append(run_id)
        return object()

    monkeypatch.setattr(RunService, "schedule_run", fake_schedule_run)
    headers = {"Idempotency-Key": "api-replay-key"}

    first = demo_client.post("/api/runs", json=RUN_BODY, headers=headers)
    replay = demo_client.post("/api/runs", json=RUN_BODY, headers=headers)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert scheduled == [first.json()["id"]]


def test_run_idempotency_key_rejects_different_normalized_request(
    demo_client: TestClient,
    monkeypatch: Any,
) -> None:
    """A key may replay its original normalized request only."""
    executions: list[str] = []

    async def fake_execute_run(
        self: RunService, run_id: str, input_data: Any
    ) -> dict[str, Any]:
        del self, input_data
        executions.append(run_id)
        return {"status": "pending"}

    monkeypatch.setattr(RunService, "execute_run", fake_execute_run)
    headers = {"Idempotency-Key": "api-conflict-key"}
    first_request = {"user_request": "  first request  "}
    second_request = {"user_request": "second request"}

    first = demo_client.post("/api/runs", json=first_request, headers=headers)
    conflict = demo_client.post("/api/runs", json=second_request, headers=headers)

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
    assert executions == [first.json()["id"]]


def test_runs_without_idempotency_key_are_independent(
    demo_client: TestClient,
    monkeypatch: Any,
) -> None:
    """Requests without a key create and execute independent runs."""
    scheduled: list[str] = []

    def fake_schedule_run(
        self: RunService, run_id: str, input_data: Any
    ) -> object:
        del self, input_data
        scheduled.append(run_id)
        return object()

    monkeypatch.setattr(RunService, "schedule_run", fake_schedule_run)

    first = demo_client.post("/api/runs", json=RUN_BODY)
    second = demo_client.post("/api/runs", json=RUN_BODY)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert scheduled == [first.json()["id"], second.json()["id"]]


def test_run_idempotency_key_must_not_be_blank(demo_client: TestClient) -> None:
    """An explicitly supplied blank key is invalid rather than auto-generated."""
    response = demo_client.post(
        "/api/runs", json=RUN_BODY, headers={"Idempotency-Key": "  "}
    )
    assert response.status_code == 422
