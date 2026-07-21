"""Regression tests for sync and async integration-test loop isolation."""

from __future__ import annotations

import asyncio

import pytest


def test_sync_test_can_clear_the_policy_event_loop() -> None:
    """Model a synchronous TestClient test clearing the policy's current loop."""

    async def _complete() -> None:
        await asyncio.sleep(0)

    asyncio.run(_complete())

    with pytest.raises(RuntimeError, match="There is no current event loop"):
        asyncio.get_event_loop()


@pytest.mark.asyncio
async def test_async_test_after_sync_test_uses_session_loop(
    _session_event_loop: asyncio.AbstractEventLoop,
) -> None:
    """The registered setup hook restores the session loop after asyncio.run."""
    await asyncio.sleep(0)
    assert asyncio.get_event_loop() is _session_event_loop


def test_ensure_current_event_loop_repairs_policy_loop() -> None:
    """The helper installs a usable current loop after asyncio.run clears it."""
    from . import conftest

    asyncio.run(asyncio.sleep(0))
    assert conftest._current_event_loop() is None

    loop = conftest._ensure_current_event_loop()

    assert conftest._current_event_loop() is loop
    assert not loop.is_closed()


def test_close_owned_event_loops_discards_failed_loops_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup removes failed loops and still closes every other owned loop."""
    from . import conftest

    class _FakeLoop:
        def __init__(self, close_error: RuntimeError | None = None) -> None:
            self.close_error = close_error
            self.close_attempts = 0
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_error is not None:
                raise self.close_error
            self.closed = True

    failed_loop = _FakeLoop(RuntimeError("loop is running"))
    healthy_loop = _FakeLoop()
    owned_loops = {failed_loop, healthy_loop}
    conftest._owned_event_loops.update(owned_loops)
    monkeypatch.setattr(conftest, "_current_event_loop", lambda: None)

    try:
        with pytest.raises(
            ExceptionGroup, match="Failed to close one or more harness event loops"
        ) as exc_info:
            conftest._close_owned_event_loops()

        assert exc_info.value.exceptions == (failed_loop.close_error,)
        conftest._close_owned_event_loops()

        assert failed_loop.close_attempts == 1
        assert healthy_loop.close_attempts == 1
        assert healthy_loop.closed
    finally:
        conftest._owned_event_loops.difference_update(owned_loops)


def test_close_owned_current_loop_clears_policy_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed close must still clear a current loop from the event-loop policy."""
    from . import conftest

    class _FakeLoop:
        def __init__(self) -> None:
            self.close_attempts = 0

        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            self.close_attempts += 1
            raise RuntimeError("loop is running")

    current_loop = _FakeLoop()
    conftest._owned_event_loops.add(current_loop)
    monkeypatch.setattr(conftest, "_current_event_loop", lambda: current_loop)
    set_event_loop_calls: list[asyncio.AbstractEventLoop | None] = []
    monkeypatch.setattr(asyncio, "set_event_loop", set_event_loop_calls.append)

    try:
        with pytest.raises(
            ExceptionGroup, match="Failed to close one or more harness event loops"
        ):
            conftest._close_owned_event_loops()

        assert set_event_loop_calls == [None]
        assert current_loop.close_attempts == 1
        assert current_loop not in conftest._owned_event_loops
    finally:
        conftest._owned_event_loops.discard(current_loop)


def test_restore_event_loop_fails_for_closed_session_loop() -> None:
    """A closed pytest-asyncio session loop must never get a fallback loop."""
    from . import conftest

    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    with pytest.raises(
        RuntimeError,
        match="pytest-asyncio session event loop is closed during active test execution",
    ):
        conftest._restore_event_loop(closed_loop)
