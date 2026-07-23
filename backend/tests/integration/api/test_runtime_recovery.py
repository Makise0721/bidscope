"""Durable-checkpoint API lifecycle and cross-process recovery coverage."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import sqlalchemy as sa
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.main import create_app
from bidscope.persistence.models import QueryRun, RunEvent
from fastapi.testclient import TestClient

SCHEDULED_QUERY = "每周一上午 9 点，汇总近 7 天四川省的服务器招标"


def _poll_status(client: TestClient, run_id: str, expected: str) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] == expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {expected!r}: {last}")


def _run_in_client(
    client: TestClient,
    operation: Callable[[Any], Awaitable[Any]],
) -> Any:
    assert client.portal is not None
    return client.portal.call(operation, client.app.state.run_service)


async def _run_identity(service: Any, run_id: str) -> tuple[str, str, str | None]:
    async with service.session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    return str(run.id), run.run_key, run.checkpoint_thread_id


async def _event_nodes(service: Any, run_id: str) -> list[str]:
    async with service.session_factory() as session:
        result = await session.execute(
            sa.select(RunEvent.node)
            .where(RunEvent.query_run_id == run_id)
            .order_by(RunEvent.seq)
        )
        return list(result.scalars())


async def _completed_persistence(service: Any, run_id: str) -> QueryRun:
    async with service.session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    return run


def test_test_mode_api_uses_postgres_checkpointer(test_settings: Settings) -> None:
    """The deployed API route is backed by a lifespan-owned PostgreSQL saver."""
    with TestClient(create_app(settings=test_settings)) as client:
        service = client.app.state.run_service
        assert service.checkpointer_kind == "postgres"

        created = client.post("/api/runs", json={"user_request": "四川服务器招标"})
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        _poll_status(client, run_id, "completed")
        persisted = _run_in_client(client, lambda service: _completed_persistence(service, run_id))
        assert persisted.search_intent
        assert persisted.token_usage
        assert persisted.completed_at is not None


def test_startup_marks_only_stale_runs_retryable(test_settings: Settings) -> None:
    """Recovery runs after the service is valid and leaves terminal/pause rows alone."""
    ids = {status: str(uuid.uuid4()) for status in (
        "pending", "running", "completed", "awaiting_confirmation",
    )}

    async def seed() -> None:
        engine, session_factory = create_engine_and_session(test_settings)
        try:
            async with session_factory() as session:
                for status, run_id in ids.items():
                    session.add(QueryRun(
                        id=run_id,
                        run_key=run_id,
                        status=status,
                        user_request="startup recovery",
                        checkpoint_thread_id=run_id,
                    ))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())

    with TestClient(create_app(settings=test_settings)) as client:
        async def statuses(service: Any) -> dict[str, str]:
            async with service.session_factory() as session:
                result = await session.execute(
                    sa.select(QueryRun.id, QueryRun.status).where(QueryRun.id.in_(ids.values()))
                )
            return {str(run_id): status for run_id, status in result.all()}

        recovered = _run_in_client(client, statuses)
        assert client.app.state.run_service.checkpointer_kind == "postgres"

    assert recovered[ids["pending"]] == "retryable"
    assert recovered[ids["running"]] == "retryable"
    assert recovered[ids["completed"]] == "completed"
    assert recovered[ids["awaiting_confirmation"]] == "awaiting_confirmation"


def test_confirmation_resumes_checkpoint_from_a_fresh_api_process(
    test_settings: Settings,
) -> None:
    """Process B confirms process A's pause without duplicate upstream events."""
    with TestClient(create_app(settings=test_settings)) as process_a:
        created = process_a.post("/api/runs", json={"user_request": SCHEDULED_QUERY})
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        _poll_status(process_a, run_id, "awaiting_confirmation")
        identity = _run_in_client(process_a, lambda service: _run_identity(service, run_id))
        events_after_a = _run_in_client(process_a, lambda service: _event_nodes(service, run_id))

    with TestClient(create_app(settings=test_settings)) as process_b:
        confirmed = process_b.post(f"/api/runs/{run_id}/confirm", json={"action": "approve"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json() == {"id": run_id, "status": "completed"}
        _poll_status(process_b, run_id, "completed")
        assert _run_in_client(process_b, lambda service: _run_identity(service, run_id)) == identity
        events_after_b = _run_in_client(process_b, lambda service: _event_nodes(service, run_id))

    assert events_after_b[:len(events_after_a)] == events_after_a
    assert not {node: count for node, count in Counter(events_after_b).items() if count > 1}


def test_retry_resumes_interrupted_checkpoint_on_its_original_thread(
    test_settings: Settings,
) -> None:
    """A retryable pause resumes on the persisted thread instead of a new run."""
    with TestClient(create_app(settings=test_settings)) as process_a:
        created = process_a.post("/api/runs", json={"user_request": SCHEDULED_QUERY})
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        _poll_status(process_a, run_id, "awaiting_confirmation")
        identity = _run_in_client(process_a, lambda service: _run_identity(service, run_id))
        events_after_a = _run_in_client(process_a, lambda service: _event_nodes(service, run_id))

    async def mark_retryable() -> None:
        engine, session_factory = create_engine_and_session(test_settings)
        try:
            async with session_factory() as session:
                await session.execute(
                    sa.update(QueryRun).where(QueryRun.id == run_id).values(status="retryable")
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(mark_retryable())

    with TestClient(create_app(settings=test_settings)) as process_b:
        retried = process_b.post(f"/api/runs/{run_id}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json() == {"id": run_id, "status": "completed"}
        _poll_status(process_b, run_id, "completed")
        assert _run_in_client(process_b, lambda service: _run_identity(service, run_id)) == identity
        events_after_b = _run_in_client(process_b, lambda service: _event_nodes(service, run_id))

    assert events_after_b[:len(events_after_a)] == events_after_a
    assert not {node: count for node, count in Counter(events_after_b).items() if count > 1}
