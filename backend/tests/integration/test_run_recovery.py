"""Startup recovery of stale query runs.

A process crash can leave ``query_runs`` rows stuck in ``running``. On startup
we must flip those stale rows to ``retryable`` so they can be explicitly
restarted, while leaving their Postgres checkpoints intact for an explicit
resume.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from bidscope.graph.executor import create_run, mark_stale_runs_retryable
from bidscope.persistence.models import QueryRun


@pytest.mark.asyncio
async def test_mark_stale_runs_retryable(session_factory: Any) -> None:
    """Stale ``running`` rows become ``retryable``; other statuses are untouched."""
    running_id = str(uuid.uuid4())
    pending_id = str(uuid.uuid4())
    success_id = str(uuid.uuid4())

    async with session_factory() as session:
        for run_id, status in (
            (running_id, "running"),
            (pending_id, "pending"),
            (success_id, "success"),
        ):
            session.add(QueryRun(
                id=run_id, run_key=run_id, status=status, user_request="x",
            ))
        await session.commit()

    changed = await mark_stale_runs_retryable(session_factory=session_factory)
    assert changed == 1

    async with session_factory() as session:
        result = await session.execute(
            sa.select(QueryRun.id, QueryRun.status).where(
                QueryRun.id.in_([running_id, pending_id, success_id])
            )
        )
        # ``QueryRun.id`` is a UUID column; normalise keys to strings for lookup.
        by_id = {str(run_id): status for run_id, status in result.all()}

    assert by_id[running_id] == "retryable"
    assert by_id[pending_id] == "pending"
    assert by_id[success_id] == "success"


@pytest.mark.asyncio
async def test_create_run_starts_in_pending(session_factory: Any) -> None:
    """``create_run`` persists a run row and a usable thread_id."""
    run_id = await create_run("my request", session_factory=session_factory)
    async with session_factory() as session:
        row = await session.get(QueryRun, run_id)
    assert row is not None
    assert row.status == "pending"
    assert row.user_request == "my request"
    assert row.checkpoint_thread_id == run_id
