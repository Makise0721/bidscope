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
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from bidscope.api.dependencies import RunService
from bidscope.config import get_settings
from bidscope.graph.executor import create_run, mark_stale_runs_retryable
from bidscope.persistence.models import QueryRun, RunEvent


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
            session.add(
                QueryRun(
                    id=run_id,
                    run_key=run_id,
                    status=status,
                    user_request="x",
                    updated_at=stale_at,
                )
            )
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
        create_run("same request", run_key="concurrent-key", session_factory=session_factory),
        create_run("same request", run_key="concurrent-key", session_factory=session_factory),
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
        session.add(
            QueryRun(
                id=run_id,
                run_key="concurrent-retry",
                status="retryable",
                user_request="retry request",
            )
        )
        await session.commit()

    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    release_execution = asyncio.Event()
    executions: list[str] = []
    force_fresh_values: list[bool] = []

    async def blocking_execute_run(
        self: RunService,
        claimed_run_id: str,
        input_data: Any,
        *,
        force_fresh: bool = False,
    ) -> dict[str, Any]:
        del self, input_data
        executions.append(claimed_run_id)
        force_fresh_values.append(force_fresh)
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
    assert force_fresh_values == [True]
    assert sum(isinstance(result, dict) for result in results) == 1
    conflicts = [result for result in results if getattr(result, "status_code", None) == 409]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_retry_reexecutes_fresh_input_for_terminal_checkpoint_and_preserves_events(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retryable row ignores a stale terminal checkpoint and runs fresh input."""
    run_id = str(uuid.uuid4())
    old_event = {
        "timestamp": "2026-07-18T09:00:00+00:00",
        "node": "old_node",
        "event": "old_event",
        "status": "ok",
    }
    new_event = {
        "timestamp": "2026-07-18T09:01:00+00:00",
        "node": "new_node",
        "event": "new_event",
        "status": "ok",
    }
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key="terminal-checkpoint-retry",
                status="retryable",
                user_request="fresh request",
                checkpoint_thread_id="same-thread",
            )
        )
        session.add(
            RunEvent(
                query_run_id=run_id,
                seq=0,
                timestamp=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
                node=old_event["node"],
                event=old_event["event"],
                status=old_event["status"],
            )
        )
        await session.commit()

    class TerminalCheckpointGraph:
        def __init__(self) -> None:
            self.inputs: list[Any] = []
            self.configs: list[Any] = []

        async def aget_state(self, config: Any) -> Any:
            self.configs.append(config)
            return SimpleNamespace(
                values={"status": "completed", "node_events": [old_event]},
                next=(),
            )

        async def astream(self, input_data: Any, config: Any, stream_mode: str) -> Any:
            assert stream_mode == "values"
            self.inputs.append(input_data)
            self.configs.append(config)
            yield {"status": "completed", "node_events": [old_event, new_event]}

    graph = TerminalCheckpointGraph()
    service = RunService(
        session_factory=session_factory,
        graph=graph,
        object_store=object(),
        settings=get_settings(),
    )

    from bidscope.api import dependencies
    from bidscope.graph.executor import execute as graph_execute

    force_fresh_values: list[bool] = []

    async def execute_with_mode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        force_fresh_values.append(kwargs.get("force_fresh", False))
        return await graph_execute(*args, **kwargs)

    monkeypatch.setattr(dependencies, "execute", execute_with_mode)
    result = await service.retry(run_id)

    assert result["status"] == "completed"
    assert force_fresh_values == [True]
    assert len(graph.inputs) == 1
    assert graph.inputs[0]["user_request"] == "fresh request"
    assert graph.inputs[0]["run_id"] == run_id
    assert all(config["configurable"]["thread_id"] == "same-thread" for config in graph.configs)
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        events = await session.execute(
            sa.select(RunEvent).where(RunEvent.query_run_id == run_id).order_by(RunEvent.seq)
        )
    assert run is not None
    assert run.status == "completed"
    assert [event.node for event in events.scalars()] == ["old_node", "new_node"]


@pytest.mark.asyncio
async def test_successful_completion_clears_old_error_but_keeps_degraded_errors(
    session_factory: Any,
) -> None:
    """Successful clean results clear stale errors while DOCX degradation remains visible."""
    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    clear_id = str(uuid.uuid4())
    degraded_id = str(uuid.uuid4())
    old_error = {"code": "graph_node_error", "message": "previous failure"}
    degraded_error = {
        "code": "delivery_error",
        "message": "DOCX storage unavailable",
        "details": {"docx_retryable": True},
    }
    async with session_factory() as session:
        session.add_all(
            [
                QueryRun(
                    id=clear_id,
                    run_key="clear-old-error",
                    status="retryable",
                    user_request="clear error",
                    error=old_error,
                ),
                QueryRun(
                    id=degraded_id,
                    run_key="retain-degraded-error",
                    status="retryable",
                    user_request="retain error",
                    error=old_error,
                ),
            ]
        )
        await session.commit()

    await service._update_status(clear_id, "completed", result={"status": "completed"})
    await service._update_status(
        degraded_id,
        "completed",
        result={"status": "completed", "errors": [degraded_error]},
    )

    async with session_factory() as session:
        clear_run = await session.get(QueryRun, clear_id)
        degraded_run = await session.get(QueryRun, degraded_id)
    assert clear_run is not None
    assert clear_run.status == "completed"
    assert clear_run.error is None
    assert degraded_run is not None
    assert degraded_run.status == "completed"
    assert degraded_run.error == {"errors": [degraded_error]}


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
        "failing request",
        run_key="executor-failure",
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
    status_update_started = asyncio.Event()
    release_status_update = asyncio.Event()

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
    update_status = service._update_status

    async def delayed_update_status(*args: Any, **kwargs: Any) -> None:
        status_update_started.set()
        await release_status_update.wait()
        await update_status(*args, **kwargs)

    monkeypatch.setattr(service, "_update_status", delayed_update_status)
    run_id, created = await service.create_run("shutdown request", run_key="shutdown")
    assert created is True

    task = service.schedule_run(run_id, {"user_request": "shutdown request"})
    await asyncio.wait_for(started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(service.shutdown())
    await asyncio.wait_for(status_update_started.wait(), timeout=1)
    task.cancel()
    release_status_update.set()
    await asyncio.wait_for(shutdown_task, timeout=1)

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
async def test_cancelled_retry_after_claim_repairs_run_for_next_retry(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after the claim commits restores retryable eligibility."""
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key="cancelled-retry-claim",
                status="retryable",
                user_request="retry request",
            )
        )
        await session.commit()

    class FailingStateGraph:
        async def aget_state(self, config: Any) -> None:
            del config
            raise RuntimeError("checkpoint unavailable")

    service = RunService(
        session_factory=session_factory,
        graph=FailingStateGraph(),
        object_store=object(),
        settings=get_settings(),
    )
    original_claim = service._claim_run
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()

    async def delayed_claim(
        claimed_run_id: str,
        eligible_status: str,
        status_name: str,
    ) -> bool:
        claimed = await original_claim(claimed_run_id, eligible_status, status_name)
        claim_committed.set()
        await release_claim.wait()
        return claimed

    monkeypatch.setattr(service, "_claim_run", delayed_claim)
    retry_task = asyncio.create_task(service.retry(run_id))
    await asyncio.wait_for(claim_committed.wait(), timeout=1)
    retry_task.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await retry_task

    async with session_factory() as session:
        cancelled_run = await session.get(QueryRun, run_id)
    assert cancelled_run is not None
    assert cancelled_run.status == "retryable"
    assert cancelled_run.error == {
        "code": "graph_node_error",
        "message": "retry claim cancelled",
        "details": {},
    }

    second = await service.retry(run_id)
    assert second["status"] == "retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "eligible_status", "cancellation_message"),
    [
        ("retry", "retryable", "retry claim cancelled"),
        (
            "confirm",
            "awaiting_confirmation",
            "confirmation claim cancelled",
        ),
    ],
)
async def test_claim_cancellation_reinjected_after_drain_restores_eligibility(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    eligible_status: str,
    cancellation_message: str,
) -> None:
    """A cancellation queued between claim drain and repair cannot strand the run."""
    from bidscope.api import dependencies

    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key=f"reinjected-{method_name}-claim",
                status=eligible_status,
                user_request="retry request",
            )
        )
        await session.commit()

    service = RunService(
        session_factory=session_factory,
        graph=object(),
        object_store=object(),
        settings=get_settings(),
    )
    original_claim = service._claim_run
    original_drain = dependencies._drain_task_preserving_cancellation
    original_repair = service._repair_cancelled_claim
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()

    async def delayed_claim(
        claimed_run_id: str,
        claimable_status: str,
        claim_status_name: str,
    ) -> bool:
        claimed = await original_claim(claimed_run_id, claimable_status, claim_status_name)
        claim_committed.set()
        await release_claim.wait()
        return claimed

    async def drain_then_reinject_cancellation(
        claim: asyncio.Future[bool],
    ) -> bool:
        claimed = await original_drain(claim)
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        return claimed

    async def repair_after_cancellation_delivery(
        repaired_run_id: str,
        message: str,
    ) -> None:
        await asyncio.sleep(0)
        await original_repair(repaired_run_id, message)

    monkeypatch.setattr(service, "_claim_run", delayed_claim)
    monkeypatch.setattr(service, "_repair_cancelled_claim", repair_after_cancellation_delivery)
    monkeypatch.setattr(
        dependencies,
        "_drain_task_preserving_cancellation",
        drain_then_reinject_cancellation,
    )

    cancelled_operation = asyncio.create_task(getattr(service, method_name)(run_id))
    await asyncio.wait_for(claim_committed.wait(), timeout=1)
    cancelled_operation.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_operation

    async with session_factory() as session:
        cancelled_run = await session.get(QueryRun, run_id)
    assert cancelled_run is not None
    assert cancelled_run.status == "retryable"
    assert cancelled_run.error == {
        "code": "graph_node_error",
        "message": cancellation_message,
        "details": {},
    }
    assert await original_claim(run_id, "retryable", "retryable") is True


@pytest.mark.asyncio
async def test_cancelled_retry_get_run_repairs_run_for_next_retry(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the post-claim row lookup restores eligibility."""
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key="cancelled-retry-get-run",
                status="retryable",
                user_request="retry request",
            )
        )
        await session.commit()

    class FailingStateGraph:
        async def aget_state(self, config: Any) -> None:
            del config
            raise RuntimeError("checkpoint unavailable")

    service = RunService(
        session_factory=session_factory,
        graph=FailingStateGraph(),
        object_store=object(),
        settings=get_settings(),
    )
    original_get_run = service.get_run
    get_run_started = asyncio.Event()
    release_get_run = asyncio.Event()

    async def delayed_get_run(looked_up_run_id: str) -> Any:
        get_run_started.set()
        await release_get_run.wait()
        return await original_get_run(looked_up_run_id)

    monkeypatch.setattr(service, "get_run", delayed_get_run)
    retry_task = asyncio.create_task(service.retry(run_id))
    await asyncio.wait_for(get_run_started.wait(), timeout=1)
    retry_task.cancel()
    release_get_run.set()
    with pytest.raises(asyncio.CancelledError):
        await retry_task

    async with session_factory() as session:
        cancelled_run = await session.get(QueryRun, run_id)
    assert cancelled_run is not None
    assert cancelled_run.status == "retryable"
    assert cancelled_run.error == {
        "code": "graph_node_error",
        "message": "retry checkpoint lookup cancelled",
        "details": {},
    }

    second = await service.retry(run_id)
    assert second["status"] == "retryable"


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
        session.add(
            QueryRun(
                id=run_id,
                run_key="retry-state-failure",
                status="retryable",
                user_request="retry request",
            )
        )
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


@pytest.mark.asyncio
async def test_cancelled_retry_checkpoint_lookup_repairs_run_for_next_retry(
    session_factory: Any,
) -> None:
    """Cancellation during checkpoint lookup restores retryable eligibility."""

    class CancelledStateGraph:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.never_finishes = asyncio.Event()
            self.calls = 0

        async def aget_state(self, config: Any) -> None:
            del config
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.never_finishes.wait()
            raise RuntimeError("checkpoint unavailable")

    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key="cancelled-retry-state",
                status="retryable",
                user_request="retry request",
            )
        )
        await session.commit()

    graph = CancelledStateGraph()
    service = RunService(
        session_factory=session_factory,
        graph=graph,
        object_store=object(),
        settings=get_settings(),
    )

    retry_task = asyncio.create_task(service.retry(run_id))
    await asyncio.wait_for(graph.started.wait(), timeout=1)
    retry_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retry_task

    async with session_factory() as session:
        cancelled_run = await session.get(QueryRun, run_id)
    assert cancelled_run is not None
    assert cancelled_run.status == "retryable"
    assert cancelled_run.error == {
        "code": "graph_node_error",
        "message": "retry checkpoint lookup cancelled",
        "details": {},
    }

    second = await service.retry(run_id)

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
