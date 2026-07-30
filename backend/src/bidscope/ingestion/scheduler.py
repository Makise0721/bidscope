"""Operational controls for the isolated ingestion process role."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from bidscope.config import Settings
from bidscope.db import create_engine_and_session

INGESTION_ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"bidscope:authorized-ingestion:v1").digest()[:8],
    byteorder="big",
    signed=True,
)
MAX_RETRY_DELAY_SECONDS = 86_400


class IngestionDisabledError(RuntimeError):
    """Raised when the isolated worker is not explicitly enabled."""


class IngestionConfigurationError(RuntimeError):
    """Raised when the operator has not supplied a runnable ingestion factory."""


def ensure_ingestion_enabled(settings: Settings) -> None:
    if settings.process_role != "ingestion" or not settings.live_ingestion_enabled:
        raise IngestionDisabledError(
            "authorized ingestion requires process_role='ingestion' and live_ingestion_enabled=true"
        )


def bounded_retry_delay_seconds(retry_after_seconds: int | None) -> int:
    """Return a positive, bounded delay so rate limits cannot cause tight loops."""
    requested = retry_after_seconds if retry_after_seconds is not None else 60
    return max(1, min(requested, MAX_RETRY_DELAY_SECONDS))


def next_eligible_ingestion_at(now: datetime, retry_after_seconds: int | None) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now + timedelta(seconds=bounded_retry_delay_seconds(retry_after_seconds))


async def acquire_ingestion_lock(connection: AsyncConnection) -> bool:
    result = await connection.execute(
        sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": INGESTION_ADVISORY_LOCK_KEY}
    )
    return bool(result.scalar_one())


async def release_ingestion_lock(connection: AsyncConnection) -> bool:
    result = await connection.execute(
        sa.text("SELECT pg_advisory_unlock(:k)"), {"k": INGESTION_ADVISORY_LOCK_KEY}
    )
    return bool(result.scalar_one())


async def run_with_ingestion_lock(
    connection: AsyncConnection,
    runner: Callable[[], Awaitable[Any]],
) -> Any:
    """Run one acquisition only while the process-level lock is held."""
    if not await acquire_ingestion_lock(connection):
        return {"status": "skipped", "reason": "ingestion_lock_not_acquired"}
    try:
        return await runner()
    finally:
        await release_ingestion_lock(connection)


async def run_ingestion_once(
    settings: Settings,
    *,
    runner_factory: Callable[[Settings], Callable[[], Awaitable[Any]]] | None = None,
) -> Any:
    """Validate role controls, acquire the worker lock, and run one acquisition."""
    ensure_ingestion_enabled(settings)
    resolved_factory = runner_factory or load_ingestion_runner_factory(settings)
    runner = resolved_factory(settings)
    if not callable(runner):
        raise IngestionConfigurationError("authorized ingestion runner is not callable")

    async def run() -> Any:
        return await runner()

    engine, _session_factory = create_engine_and_session(settings)
    try:
        async with engine.connect() as connection:
            return await run_with_ingestion_lock(connection, run)
    finally:
        await engine.dispose()


def load_ingestion_runner_factory(
    settings: Settings,
) -> Callable[[Settings], Callable[[], Awaitable[Any]]]:
    """Load the operator-supplied endpoint/signing adapter without guessing it."""
    reference = settings.ccgp_runner_factory
    if not reference or ":" not in reference:
        raise IngestionConfigurationError(
            "authorized ingestion signer and endpoint runner are not configured"
        )
    module_name, attribute = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
    except Exception:
        raise IngestionConfigurationError(
            "authorized ingestion signer and endpoint runner are not available"
        ) from None
    if not callable(factory):
        raise IngestionConfigurationError(
            "authorized ingestion runner factory is not callable"
        )
    return cast(Callable[[Settings], Callable[[], Awaitable[Any]]], factory)


async def start_ingestion_loop(
    settings: Settings,
    *,
    runner_factory: Callable[[Settings], Callable[[], Awaitable[Any]]] | None = None,
    sleep: Callable[[float], Awaitable[Any]],
) -> None:
    """Run the bounded polling loop; the caller owns graceful shutdown."""
    ensure_ingestion_enabled(settings)
    resolved_factory = runner_factory or load_ingestion_runner_factory(settings)
    while True:
        result = await run_ingestion_once(settings, runner_factory=resolved_factory)
        status = (
            result.get("status")
            if isinstance(result, dict)
            else getattr(result, "status", None)
        )
        retry_after = (
            result.get("retry_after_seconds")
            if isinstance(result, dict)
            else getattr(result, "retry_after_seconds", None)
        )
        delay = (
            bounded_retry_delay_seconds(retry_after)
            if status == "rate_limited"
            else settings.ccgp_poll_seconds
        )
        await sleep(delay)
