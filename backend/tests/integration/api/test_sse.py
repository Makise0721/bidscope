"""Integration tests for the run-events SSE stream.

``GET /api/runs/{id}/events`` streams the persisted ``run_events`` rows in ``seq``
order as Server-Sent Events, honours ``Last-Event-ID``, emits periodic heartbeats,
and closes after the terminal event.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from bidscope.db import create_engine_and_session
from bidscope.persistence.models import QueryRun, RunEvent
from fastapi.testclient import TestClient

TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"


def _insert_run(status: str) -> str:
    """Create a run directly with three ordered events; return its id."""
    _, session_factory = create_engine_and_session()
    now = datetime.now(UTC)

    async def _do() -> str:
        async with session_factory() as session:
            run = QueryRun(
                run_key="sse-seed",
                status=status,
                user_request="sse demo",
                checkpoint_thread_id="sse-seed",
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            for seq, (node, event) in enumerate([
                ("parse_intent", "intent_parsed"),
                ("validate_intent", "intent_valid"),
                ("persist_and_deliver", "run_completed"),
            ]):
                session.add(RunEvent(
                    query_run_id=run.id,
                    seq=seq,
                    timestamp=now,
                    node=node,
                    event=event,
                    status="ok",
                ))
            await session.commit()
            return run.id

    return asyncio.run(_do())


def _parse_sse(body: str) -> list[dict]:
    """Parse a raw SSE body into a list of {id, event, data} dicts.

    The synthetic ``terminal`` marker the endpoint emits to close the stream is
    dropped so callers see only the real run events.
    """
    results: list[dict] = []
    current: dict = {}
    for line in body.splitlines():
        if line.startswith("id:"):
            current["id"] = line[3:].strip()
        elif line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                current["data"] = json.loads(raw)
            except json.JSONDecodeError:
                current["data"] = raw
        elif line == "":
            if current.get("event") and current.get("event") != "terminal":
                results.append(current)
            current = {}
    return results


@pytest.fixture()
def sse_run_id() -> str:
    """Seed a completed run with ordered events for SSE tests."""
    return _insert_run("completed")


def test_sse_streams_events_in_seq_order(demo_client: TestClient, sse_run_id: str) -> None:
    """The endpoint streams ordered events as SSE with id/event/data fields."""
    with demo_client.stream("GET", f"/api/runs/{sse_run_id}/events") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    assert [e["event"] for e in events] == ["intent_parsed", "intent_valid", "run_completed"]
    # Each event carries an id and a JSON data payload.
    assert all("id" in e and "data" in e for e in events)
    assert events[2]["data"]["node"] == "persist_and_deliver"


def test_sse_honors_last_event_id(demo_client: TestClient, sse_run_id: str) -> None:
    """Last-Event-ID skips already-seen events (client reconnection)."""
    with demo_client.stream(
        "GET", f"/api/runs/{sse_run_id}/events", headers={"Last-Event-ID": "0"}
    ) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")

    events = _parse_sse(body)
    # seq 0 already seen → only seq 1 and 2 are emitted.
    assert [e["event"] for e in events] == ["intent_valid", "run_completed"]


def test_sse_returns_404_for_unknown_run(demo_client: TestClient) -> None:
    """An unknown run id returns 404."""
    with demo_client.stream(
        "GET", "/api/runs/00000000-0000-0000-0000-000000000000/events"
    ) as response:
        assert response.status_code == 404
