"""Cancellation-safety unit coverage for the run lifecycle service."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bidscope.api.dependencies import RunService


class _ChildAbort(BaseException):
    """A child-task failure that does not inherit from ``Exception``."""


@pytest.mark.asyncio
async def test_shutdown_raises_unexpected_child_base_exception_after_draining() -> None:
    """Shutdown drains tracked work before surfacing a non-cancellation failure."""
    service = object.__new__(RunService)
    service._shutting_down = False
    started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def abort_when_cancelled() -> dict[str, Any]:
        started.set()
        try:
            await never_finishes.wait()
        except asyncio.CancelledError as cancellation_error:
            raise _ChildAbort("shutdown child aborted") from cancellation_error
        return {}

    task = asyncio.create_task(abort_when_cancelled())
    service._run_tasks = {task}
    await asyncio.wait_for(started.wait(), timeout=1)

    with pytest.raises(_ChildAbort, match="shutdown child aborted"):
        await service.shutdown()

    assert task.done()
    assert not service._run_tasks
    assert service._shutting_down is True


@pytest.mark.asyncio
async def test_claim_cancellation_drains_repair_after_cancellation_reinjection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed claim completes repair despite cancellation re-injected by drain."""
    service = object.__new__(RunService)
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    repair_started = asyncio.Event()
    release_repair = asyncio.Event()
    repair_finished = asyncio.Event()

    async def blocked_claim(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        claim_started.set()
        await release_claim.wait()
        return True

    async def blocked_repair(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        repair_started.set()
        await release_repair.wait()
        repair_finished.set()

    monkeypatch.setattr(service, "_claim_run", blocked_claim)
    monkeypatch.setattr(service, "_repair_cancelled_claim", blocked_repair)

    task = asyncio.create_task(
        service._claim_run_safely("run-1", "retryable", "retryable", "cancelled")
    )
    await asyncio.wait_for(claim_started.wait(), timeout=1)
    task.cancel()
    release_claim.set()
    await asyncio.wait_for(repair_started.wait(), timeout=1)
    release_repair.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert repair_finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [True, False])
async def test_claim_cancellation_repairs_only_a_committed_claim(
    monkeypatch: pytest.MonkeyPatch,
    claimed: bool,
) -> None:
    """A cancelled claim child repairs only when its CAS committed the row."""
    service = object.__new__(RunService)
    started = asyncio.Event()
    release = asyncio.Event()
    repaired = AsyncMock()

    async def blocked_claim(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        started.set()
        await release.wait()
        return claimed

    monkeypatch.setattr(service, "_claim_run", blocked_claim)
    monkeypatch.setattr(service, "_repair_cancelled_claim", repaired)

    task = asyncio.create_task(
        service._claim_run_safely("run-1", "retryable", "retryable", "cancelled")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    if claimed:
        repaired.assert_awaited_once_with("run-1", "cancelled")
    else:
        repaired.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_cancellation_during_get_run_repairs_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation in the post-claim row lookup invokes claim repair."""
    service = object.__new__(RunService)
    get_run_started = asyncio.Event()
    release_get_run = asyncio.Event()
    repaired = AsyncMock()

    async def get_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        get_run_started.set()
        await release_get_run.wait()
        return SimpleNamespace(
            id="run-1",
            checkpoint_thread_id=None,
            user_request="request",
        )

    monkeypatch.setattr(service, "_claim_run_safely", AsyncMock())
    monkeypatch.setattr(service, "get_run", get_run)
    monkeypatch.setattr(service, "_repair_cancelled_claim", repaired)

    task = asyncio.create_task(service.retry("run-1"))
    await asyncio.wait_for(get_run_started.wait(), timeout=1)
    task.cancel()
    release_get_run.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    repaired.assert_awaited_once_with("run-1", "retry checkpoint lookup cancelled")


@pytest.mark.asyncio
async def test_persist_cancellation_chains_non_exception_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cancellation BaseException from persistence is never ignored."""
    service = object.__new__(RunService)

    async def failing_update_status(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise _ChildAbort("status persistence aborted")

    monkeypatch.setattr(service, "_update_status", failing_update_status)

    with pytest.raises(asyncio.CancelledError) as error:
        await service._persist_cancellation("run-1", {"code": "graph_node_error"})

    assert isinstance(error.value.__cause__, _ChildAbort)
