"""Startup recovery of stale query runs.

A process crash can leave ``query_runs`` rows stuck in ``running``. On startup
we must flip those stale rows to ``retryable`` so they can be explicitly
restarted, while leaving their Postgres checkpoints intact for an explicit
resume.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from bidscope.api.dependencies import RunService
from bidscope.config import get_settings
from bidscope.graph.executor import create_run, mark_stale_runs_retryable
from bidscope.persistence.models import QueryRun


@pytest.mark.asyncio
async def test_mark_stale_runs_retryable(session_factory: Any) -> None:
    """Stale ``pending`` and ``running`` rows become ``retryable``."""
    stale_before = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    stale_at = stale_before - timedelta(seconds=1)
    running_id = str(uuid.uuid4())
    pending_id = str(uuid.uuid4())
    completed_id = str(uuid.uuid4())
    awaiting_id = str(uuid.uuid4())

    async with session_factory() as session:
        for run_id, status in (
            (running_id, "running"),
            (pending_id, "pending"),
            (completed_id, "completed"),
            (awaiting_id, "awaiting_confirmation"),
        ):
            session.add(QueryRun(
                id=run_id,
                run_key=run_id,
                status=status,
                user_request="x",
                updated_at=stale_at,
            ))
        await session.commit()

    changed = await mark_stale_runs_retryable(
        session_factory=session_factory,
        stale_before=stale_before,
    )
    assert changed == 2

    async with session_factory() as session:
        result = await session.execute(
            sa.select(QueryRun.id, QueryRun.status).where(
                QueryRun.id.in_([running_id, pending_id, completed_id, awaiting_id])
            )
        )
        # ``QueryRun.id`` is a UUID column; normalise keys to strings for lookup.
        by_id = {str(run_id): status for run_id, status in result.all()}

    assert by_id[running_id] == "retryable"
    assert by_id[pending_id] == "retryable"
    assert by_id[completed_id] == "completed"
    assert by_id[awaiting_id] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_create_run_starts_in_pending(session_factory: Any) -> None:
    """``create_run`` persists a run row and a usable thread_id."""
    run_id, created = await create_run(
        "my request", run_key="recovery-key", session_factory=session_factory
    )
    assert created is True

    replay_id, replay_created = await create_run(
        "my request", run_key="recovery-key", session_factory=session_factory
    )
    assert replay_id == run_id
    assert replay_created is False

    async with session_factory() as session:
        row = await session.get(QueryRun, run_id)
    assert row is not None
    assert row.status == "pending"
    assert row.user_request == "my request"
    assert row.checkpoint_thread_id == run_id


@pytest.mark.asyncio
async def test_create_run_concurrent_same_key_returns_one_row(session_factory: Any) -> None:
    """Concurrent callers sharing a key receive one durable run."""
    results = await asyncio.gather(
        create_run(
            "same request", run_key="concurrent-key", session_factory=session_factory
        ),
        create_run(
            "same request", run_key="concurrent-key", session_factory=session_factory
        ),
    )

    assert len({run_id for run_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count()).where(QueryRun.run_key == "concurrent-key")
        )
    assert count == 1


@pytest.mark.asyncio
async def test_retry_claims_run_before_concurrent_execution(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent retries claim one row, preventing duplicate event persistence."""
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id,
            run_key="concurrent-retry",
            status="retryable",
            user_request="retry request",
        ))
        await session.commit()

    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    release_execution = asyncio.Event()
    executions: list[str] = []

    async def blocking_execute_run(
        self: RunService, claimed_run_id: str, input_data: Any
    ) -> dict[str, Any]:
        del self, input_data
        executions.append(claimed_run_id)
        await release_execution.wait()
        return {"status": "completed"}

    monkeypatch.setattr(RunService, "execute_run", blocking_execute_run)

    retries = [
        asyncio.create_task(service.retry(run_id)),
        asyncio.create_task(service.retry(run_id)),
    ]
    await asyncio.sleep(0.05)
    release_execution.set()
    results = await asyncio.gather(*retries, return_exceptions=True)

    assert executions == [run_id]
    assert sum(isinstance(result, dict) for result in results) == 1
    conflicts = [result for result in results if getattr(result, "status_code", None) == 409]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_execute_run_persists_retryable_when_executor_raises(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route-style detached task resolves after an unexpected graph failure."""
    from bidscope.api import dependencies

    async def exploding_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise RuntimeError("executor failure")

    monkeypatch.setattr(dependencies, "execute", exploding_execute)
    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    run_id, created = await service.create_run(
        "failing request", run_key="executor-failure",
    )
    assert created is True

    task = asyncio.create_task(
        service.execute_run(run_id, {"user_request": "failing request"}),
    )
    result = await task

    assert result["status"] == "retryable"
    assert task.exception() is None
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    assert run.status == "retryable"
    assert run.error == {
        "code": "graph_node_error",
        "message": "executor failure",
        "details": {},
    }


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_scheduled_runs_before_disposal(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a tracked run persists a retryable status before shutdown returns."""
    from bidscope.api import dependencies

    started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocking_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        started.set()
        await never_finishes.wait()
        return {"status": "completed"}

    monkeypatch.setattr(dependencies, "execute", blocking_execute)
    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    run_id, created = await service.create_run("shutdown request", run_key="shutdown")
    assert created is True

    task = service.schedule_run(run_id, {"user_request": "shutdown request"})
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(service.shutdown(), timeout=1)

    assert task.cancelled()
    assert not service._run_tasks
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    assert run.status == "retryable"
    assert run.error == {
        "code": "graph_node_error",
        "message": "run execution cancelled",
        "details": {},
    }


@pytest.mark.asyncio
async def test_retry_checkpoint_state_failure_leaves_run_eligible_for_retry(
    session_factory: Any,
) -> None:
    """A failing checkpoint state query cannot strand a claimed run in ``running``."""
    class FailingStateGraph:
        def __init__(self) -> None:
            self.calls = 0

        async def aget_state(self, config: Any) -> None:
            del config
            self.calls += 1
            raise RuntimeError("checkpoint unavailable")

    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id,
            run_key="retry-state-failure",
            status="retryable",
            user_request="retry request",
        ))
        await session.commit()

    graph = FailingStateGraph()
    service = RunService(
        session_factory=session_factory,
        graph=graph,
        object_store=object(),
        settings=get_settings(),
    )

    first = await service.retry(run_id)
    second = await service.retry(run_id)

    assert first["status"] == "retryable"
    assert second["status"] == "retryable"
    assert graph.calls == 2
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    assert run.status == "retryable"
    assert run.error == {
        "code": "graph_node_error",
        "message": "checkpoint unavailable",
        "details": {},
    }
