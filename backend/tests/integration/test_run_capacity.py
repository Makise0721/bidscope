"""Integration tests for concurrent run capacity limits.

Exercises the ``max_concurrent_runs`` guard in :class:`RunService` and the
HTTP 429 mapping in the ``POST /api/runs`` route.

These tests require a running PostgreSQL instance (the same test database used
by the rest of the integration suite).  The session-scoped ``env_guard`` in
``backend/tests/integration/conftest.py`` skips the entire suite when the
database is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from bidscope.api.dependencies import RunCapacityError, RunService
from bidscope.config import Settings, get_settings
from bidscope.delivery.objects import LocalObjectStore
from bidscope.main import create_app
from fastapi.testclient import TestClient


def _capacity_settings(tmp_path: Path) -> Settings:
    """Build demo-mode settings with ``max_concurrent_runs=1``."""
    guarded = get_settings()
    return Settings(
        app_mode="demo",
        database_url=guarded.database_url,
        checkpoint_database_url=guarded.checkpoint_database_url,
        real_model_enabled=False,
        admin_token="test-admin-token",
        object_store_root=str(tmp_path / "objects"),
        max_concurrent_runs=1,
    )


@pytest.fixture()
def capacity_client(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    """``TestClient`` wrapping an app configured with ``max_concurrent_runs=1``."""
    tmp_path = tmp_path_factory.mktemp("capacity-test")
    settings = _capacity_settings(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        yield client


@pytest.mark.asyncio
async def test_max_concurrent_runs_blocks_excess(tmp_path: Path) -> None:
    """Scheduling beyond ``max_concurrent_runs`` raises ``RunCapacityError``."""
    settings = _capacity_settings(tmp_path)
    service = RunService(
        session_factory=None,
        graph=None,
        object_store=LocalObjectStore(root=str(tmp_path / "objects")),
        settings=settings,
    )

    # First schedule succeeds, consuming the single capacity slot.
    task = service.schedule_run("run-1", {"user_request": "first request"})
    assert service._active_run_reservations == 1

    # Second schedule fails because the slot is already taken.
    with pytest.raises(RunCapacityError, match="run capacity exhausted"):
        service.schedule_run("run-2", {"user_request": "second request"})

    # Clean up the background task.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@pytest.mark.asyncio
async def test_capacity_is_released_after_run_completes(tmp_path: Path) -> None:
    """After a run completes and releases its slot, new work can be scheduled."""
    settings = _capacity_settings(tmp_path)
    service = RunService(
        session_factory=None,
        graph=None,
        object_store=LocalObjectStore(root=str(tmp_path / "objects")),
        settings=settings,
    )

    # Schedule a run, filling the single slot.
    task = service.schedule_run("run-1", {"user_request": "first request"})
    assert service._active_run_reservations == 1

    # Simulate the run completing by releasing its reservation.
    service._release_run_reservation()
    assert service._active_run_reservations == 0

    # A new schedule should now succeed.
    task2 = service.schedule_run("run-2", {"user_request": "second request"})
    assert service._active_run_reservations == 1

    # Clean up background tasks.
    task.cancel()
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.gather(task, task2, return_exceptions=True)


def test_capacity_error_maps_to_429(capacity_client: TestClient) -> None:
    """When capacity is exhausted, ``POST /api/runs`` returns 429 with Retry-After."""
    service: RunService = capacity_client.app.state.run_service

    # Exhaust the single capacity slot before the HTTP request.
    assert service._try_reserve_run() is True
    assert service._active_run_reservations == 1

    # The route creates a pending run but schedule_run hits the limit.
    response = capacity_client.post(
        "/api/runs", json={"user_request": "test request"}
    )
    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "run_capacity_exhausted"
    assert response.headers["Retry-After"] == "5"
