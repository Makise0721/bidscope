"""Startup recovery of stale query runs.

A process crash can leave ``query_runs`` rows stuck in ``running``. On startup
we must flip those stale rows to ``retryable`` so they can be explicitly
restarted, while leaving their Postgres checkpoints intact for an explicit
resume.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from bidscope.graph.executor import create_run, mark_stale_runs_retryable
from bidscope.persistence.models import QueryRun


@pytest.mark.asyncio
async def test_mark_stale_runs_retryable(session_factory: Any) -> None:
    """Stale ``pending`` and ``running`` rows become ``retryable``."""
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
                id=run_id, run_key=run_id, status=status, user_request="x",
            ))
        await session.commit()

    changed = await mark_stale_runs_retryable(session_factory=session_factory)
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
