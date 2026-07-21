"""Unit tests for persisted subscription scheduling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call

import pytest
import sqlalchemy as sa
from bidscope import cli
from bidscope.config import Settings
from bidscope.persistence.models import Subscription
from bidscope.subscriptions import scheduler


class _ScalarResult:
    def __init__(self, rows: list[Subscription]) -> None:
        self._rows = rows

    def scalars(self) -> list[Subscription]:
        return self._rows


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


class _SessionFactory:
    def __init__(self, sessions: list[object]) -> None:
        self.sessions = iter(sessions)

    def __call__(self) -> _SessionContext:
        return _SessionContext(next(self.sessions))


def _subscription(
    subscription_id: str,
    next_run: str | None,
    *,
    status: str = "active",
    intent: Any | None = None,
) -> Subscription:
    normalized_intent: Any = intent if intent is not None else {"regions": ["四川"]}
    if next_run is not None:
        assert isinstance(normalized_intent, dict)
        normalized_intent[scheduler.KEY_NEXT_RUN_AT] = next_run
    return Subscription(
        id=subscription_id,
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
        normalized_intent=cast(dict[str, Any], normalized_intent),
        status=status,
        trigger_key=f"trigger-{subscription_id}",
    )


@pytest.mark.asyncio
async def test_list_due_subscriptions_filters_future_and_invalid_rows() -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    rows = [
        _subscription("past", "2026-07-20T08:00:00+08:00"),
        _subscription("at-now", "2026-07-20T01:00:00+00:00"),
        _subscription("future", "2026-07-20T10:00:00+08:00"),
        _subscription("missing", None),
        _subscription("invalid", "not-a-timestamp"),
        _subscription("list-intent", None, intent=[scheduler.KEY_NEXT_RUN_AT]),
        _subscription("string-intent", None, intent="2026-07-20T08:00:00+08:00"),
        _subscription("number-intent", None, intent=42),
        _subscription("paused", "2026-07-20T08:00:00+08:00", status="paused"),
    ]
    session = Mock()
    session.execute = AsyncMock(return_value=_ScalarResult(rows))

    due = await scheduler.list_due_subscriptions(_SessionFactory([session]), now)

    assert [subscription.id for subscription in due] == ["past", "at-now"]
    statement = session.execute.await_args.args[0]
    assert isinstance(statement, sa.sql.Select)


@pytest.mark.asyncio
async def test_run_scheduler_tick_requests_atomic_schedule_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    subscription = _subscription("sub-1", "2026-07-20T08:00:00+08:00")
    monkeypatch.setattr(
        scheduler, "list_due_subscriptions", AsyncMock(return_value=[subscription]),
    )
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(
        run_subscription=AsyncMock(return_value={"failed": False, "skipped": False}),
    )
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 1, "ran": 1, "skipped": 0, "failed": 0}
    service.run_subscription.assert_awaited_once_with(
        "sub-1",
        scheduled_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        advance_schedule=True,
    )
    advance.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_counts_state_malformed_after_due_list_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    malformed = _subscription("malformed", "2026-07-20T08:00:00+08:00")
    valid = _subscription("valid", "2026-07-20T08:00:00+08:00")

    async def list_due(_session_factory: Any, _now: datetime) -> list[Subscription]:
        malformed.normalized_intent = cast(Any, [])
        return [malformed, valid]

    monkeypatch.setattr(scheduler, "list_due_subscriptions", list_due)
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(run_subscription=AsyncMock(return_value={"failed": False}))
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 2, "ran": 1, "skipped": 0, "failed": 1}
    service.run_subscription.assert_awaited_once_with(
        "valid",
        scheduled_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        advance_schedule=True,
    )
    advance.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_advance_subscription_next_run_persists_next_cron_occurrence() -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    subscription = _subscription("sub-1", "2026-07-20T08:00:00+08:00")
    session = Mock()
    session.get = AsyncMock(return_value=subscription)
    session.commit = AsyncMock()
    factory = _SessionFactory([session])
    next_run = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)

    compute = Mock(return_value=next_run)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("bidscope.subscriptions.service._compute_next_run", compute)
        await scheduler.advance_subscription_next_run(
            "sub-1", session_factory=factory, now=now,
        )

    compute.assert_called_once_with("0 9 * * 1", "Asia/Shanghai", after=now)
    session.commit.assert_awaited_once()
    assert subscription.normalized_intent[scheduler.KEY_NEXT_RUN_AT] == next_run.isoformat()


@pytest.mark.asyncio
async def test_run_scheduler_tick_advances_successful_and_retains_failed_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    due = [
        _subscription("success", "2026-07-20T08:00:00+08:00"),
        _subscription("failure", "2026-07-20T08:00:00+08:00"),
    ]
    list_due = AsyncMock(return_value=due)
    monkeypatch.setattr(scheduler, "list_due_subscriptions", list_due)
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(
        run_subscription=AsyncMock(
            side_effect=[
                {"failed": False, "skipped": False},
                {"failed": True, "skipped": False},
            ]
        )
    )
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 2, "ran": 1, "skipped": 0, "failed": 1}
    list_due.assert_awaited_once_with(session_factory, now)
    service.run_subscription.assert_has_awaits([
        call(
            "success",
            scheduled_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
            advance_schedule=True,
        ),
        call(
            "failure",
            scheduled_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
            advance_schedule=True,
        ),
    ])
    advance.assert_not_awaited()
    assert due[1].normalized_intent[scheduler.KEY_NEXT_RUN_AT] == (
        "2026-07-20T08:00:00+08:00"
    )
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_disposes_engine_when_due_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    monkeypatch.setattr(
        scheduler,
        "list_due_subscriptions",
        AsyncMock(side_effect=RuntimeError("list failed")),
    )

    with pytest.raises(RuntimeError, match="list failed"):
        await scheduler.run_scheduler_tick(Settings(), now=datetime.now(UTC))

    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_disposes_engine_when_service_factory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    monkeypatch.setattr(
        scheduler,
        "list_due_subscriptions",
        AsyncMock(return_value=[_subscription("sub-1", "2026-07-20T08:00:00+08:00")]),
    )
    monkeypatch.setattr(
        scheduler,
        "_build_subscription_service",
        Mock(side_effect=RuntimeError("factory failed")),
    )

    with pytest.raises(RuntimeError, match="factory failed"):
        await scheduler.run_scheduler_tick(Settings(), now=datetime.now(UTC))

    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_continues_after_run_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    due = [
        _subscription("error", "2026-07-20T08:00:00+08:00"),
        _subscription("success", "2026-07-20T08:00:00+08:00"),
    ]
    monkeypatch.setattr(scheduler, "list_due_subscriptions", AsyncMock(return_value=due))
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(
        run_subscription=AsyncMock(side_effect=[RuntimeError("boom"), {"failed": False}]),
    )
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 2, "ran": 1, "skipped": 0, "failed": 1}
    assert [call.args[0] for call in service.run_subscription.await_args_list] == [
        "error",
        "success",
    ]
    assert all(
        invocation.kwargs["advance_schedule"] is True
        for invocation in service.run_subscription.await_args_list
    )
    advance.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_counts_service_failures_without_advancing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    due = [
        _subscription("service-error", "2026-07-20T08:00:00+08:00"),
        _subscription("service-failure", "2026-07-20T08:00:00+08:00"),
    ]
    monkeypatch.setattr(scheduler, "list_due_subscriptions", AsyncMock(return_value=due))
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(
        run_subscription=AsyncMock(
            side_effect=[
                RuntimeError("service failed"),
                {"failed": True, "skipped": False},
            ]
        )
    )
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 2, "ran": 0, "skipped": 0, "failed": 2}
    assert all(
        invocation.kwargs["advance_schedule"] is True
        for invocation in service.run_subscription.await_args_list
    )
    advance.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_scheduler_tick_leaves_lock_skips_for_lock_owner_to_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped worker never overwrites the lock owner's schedule update."""
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    due = [_subscription("skipped", "2026-07-20T08:00:00+08:00")]
    monkeypatch.setattr(scheduler, "list_due_subscriptions", AsyncMock(return_value=due))
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = _SessionFactory([])
    monkeypatch.setattr(
        scheduler,
        "create_engine_and_session",
        Mock(return_value=(engine, session_factory)),
    )
    service = SimpleNamespace(
        run_subscription=AsyncMock(return_value={"failed": False, "skipped": True}),
    )
    monkeypatch.setattr(scheduler, "_build_subscription_service", Mock(return_value=service))
    advance = AsyncMock()
    monkeypatch.setattr(scheduler, "advance_subscription_next_run", advance)

    result = await scheduler.run_scheduler_tick(Settings(), now=now)

    assert result == {"due": 1, "ran": 0, "skipped": 1, "failed": 0}
    service.run_subscription.assert_awaited_once_with(
        "skipped",
        scheduled_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        advance_schedule=True,
    )
    advance.assert_not_awaited()
    engine.dispose.assert_awaited_once()


def test_scheduler_run_once_delegates_to_async_core_and_reports_counters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings()
    tick = AsyncMock(return_value={"due": 4, "ran": 2, "skipped": 1, "failed": 1})
    monkeypatch.setattr(cli, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(scheduler, "run_scheduler_tick", tick)
    monkeypatch.setattr(
        cli,
        "create_engine_and_session",
        Mock(side_effect=AssertionError("legacy DB path must not be called")),
    )
    monkeypatch.setattr(
        scheduler,
        "list_due_subscriptions",
        AsyncMock(side_effect=AssertionError("legacy due-list path must not be called")),
    )

    cli.scheduler_run_once()

    tick.assert_awaited_once_with(settings)
    assert capsys.readouterr().out == "scheduler tick: due=4 ran=2 skipped=1 failed=1\n"


def test_tick_is_synchronous_and_runs_async_core(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    tick = AsyncMock(return_value={"due": 0, "ran": 0, "skipped": 0, "failed": 0})
    monkeypatch.setattr(scheduler, "run_scheduler_tick", tick)
    original_run = asyncio.run
    run = Mock(side_effect=original_run)
    monkeypatch.setattr(scheduler.asyncio, "run", run)

    result = scheduler._tick(settings)

    assert result is None
    run.assert_called_once()
    tick.assert_awaited_once_with(settings)


def test_build_scheduler_registers_one_minute_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    scheduler_instance = Mock()
    background_scheduler = Mock(return_value=scheduler_instance)
    interval_trigger = Mock()
    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler",
        background_scheduler,
    )
    monkeypatch.setattr("apscheduler.triggers.interval.IntervalTrigger", interval_trigger)

    result = scheduler.build_scheduler(settings)

    assert result is scheduler_instance
    background_scheduler.assert_called_once_with(timezone="Asia/Shanghai")
    interval_trigger.assert_called_once_with(minutes=scheduler.TICK_MINUTES)
    scheduler_instance.add_job.assert_called_once_with(
        scheduler._tick,
        trigger=interval_trigger.return_value,
        id="subscription_tick",
        replace_existing=True,
        args=[settings],
    )
