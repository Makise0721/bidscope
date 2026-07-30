"""Tests for isolated ingestion process controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from bidscope import cli
from bidscope.config import Settings
from bidscope.ingestion import scheduler as ingestion_scheduler
from bidscope.ingestion.scheduler import (
    INGESTION_ADVISORY_LOCK_KEY,
    IngestionConfigurationError,
    IngestionDisabledError,
    bounded_retry_delay_seconds,
    next_eligible_ingestion_at,
    run_ingestion_once,
    run_with_ingestion_lock,
)
from bidscope.subscriptions.scheduler import subscription_lock_key
from typer.testing import CliRunner


def _disabled_settings() -> Settings:
    return Settings(_env_file=None, app_mode="test")


def _enabled_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_mode="test",
        process_role="ingestion",
        live_ingestion_enabled=True,
        ccgp_api_base_url="https://www.ccgp.gov.cn",
        ccgp_client_id="operator-client",
        ccgp_signing_key="operator-signing-key",
        ccgp_runner_factory="bidscope.ingestion.operator:build_runner",
        ccgp_authorization_ref="pilot-20260730",
        ccgp_data_contract_version="ccgp-authorized-v1",
        ccgp_data_owner="operator",
        ccgp_data_regions=["national"],
        ccgp_data_categories=["procurement"],
        ccgp_data_review_status="approved",
        ccgp_data_reviewed_at="2026-07-30T00:00:00Z",
        ccgp_data_update_sla="weekly",
        ccgp_data_retention_days=365,
    )


@pytest.mark.asyncio
async def test_run_once_is_disabled_by_default() -> None:
    with pytest.raises(IngestionDisabledError):
        await run_ingestion_once(_disabled_settings())


@pytest.mark.asyncio
async def test_enabled_run_requires_an_explicit_operator_runner() -> None:
    with pytest.raises(IngestionConfigurationError):
        await run_ingestion_once(_enabled_settings())


def test_ingestion_lock_is_distinct_and_retry_delay_is_bounded() -> None:
    subscription_key = subscription_lock_key("sub-1", "2026-07-30T00:00:00+00:00")
    assert subscription_key != INGESTION_ADVISORY_LOCK_KEY
    assert bounded_retry_delay_seconds(None) == 60
    assert bounded_retry_delay_seconds(0) == 1
    assert bounded_retry_delay_seconds(999_999) == 86_400
    assert next_eligible_ingestion_at(
        datetime(2026, 7, 30, tzinfo=UTC), 30
    ) == datetime(2026, 7, 30, 0, 0, 30, tzinfo=UTC)


class _FakeResult:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class _SharedLockConnection:
    held = False

    async def execute(self, statement: sa.TextClause, _params: dict[str, int]) -> _FakeResult:
        if "try_advisory_lock" in str(statement):
            if self.held:
                return _FakeResult(False)
            self.held = True
            return _FakeResult(True)
        self.held = False
        return _FakeResult(True)


class _ConnectionContext:
    def __init__(self, connection: _SharedLockConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _SharedLockConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, connection: _SharedLockConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_run_once_uses_the_database_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _SharedLockConnection()
    engine = _FakeEngine(connection)
    monkeypatch.setattr(
        ingestion_scheduler,
        "create_engine_and_session",
        lambda _settings: (engine, None),
    )

    async def runner() -> dict[str, str]:
        return {"status": "success"}

    result = await ingestion_scheduler.run_ingestion_once(
        _enabled_settings(),
        runner_factory=lambda _settings: runner,
    )

    assert result == {"status": "success"}
    assert connection.held is False
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_two_workers_share_one_nonblocking_ingestion_lock() -> None:
    connection = _SharedLockConnection()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def runner() -> dict[str, str]:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()
        return {"status": "success"}

    first = asyncio.create_task(run_with_ingestion_lock(connection, runner))
    await started.wait()
    second = await run_with_ingestion_lock(connection, runner)
    release.set()
    first_result = await first

    assert runs == 1
    assert first_result == {"status": "success"}
    assert second == {"status": "skipped", "reason": "ingestion_lock_not_acquired"}


@pytest.mark.asyncio
async def test_ingestion_loop_honors_bounded_rate_limit_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _enabled_settings()
    sleeps: list[float] = []
    calls = 0

    async def runner() -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(status="rate_limited", retry_after_seconds=30)
        raise RuntimeError("stop fixture loop")

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            return

    monkeypatch.setattr(
        "bidscope.ingestion.scheduler.run_ingestion_once",
        lambda _settings, runner_factory=None: runner(),
    )

    with pytest.raises(RuntimeError, match="stop fixture loop"):
        from bidscope.ingestion.scheduler import start_ingestion_loop

        await start_ingestion_loop(settings, runner_factory=lambda _settings: runner, sleep=sleep)

    assert sleeps == [30]


def test_ingestion_cli_run_once_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_require_startup_settings", lambda: None)
    monkeypatch.setattr(cli, "get_settings", _disabled_settings)

    result = CliRunner().invoke(cli.app, ["ingestion", "run-once"])

    assert result.exit_code == 2
    assert "live_ingestion_enabled=true" in result.output
