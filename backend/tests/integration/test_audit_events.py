from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.persistence.models import AuditEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_audit_event_commits_and_is_queryable_by_request_and_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = f"request-{uuid.uuid4()}"
    async with session_factory() as session:
        context = AuditContext(
            request_id=request_id,
            method="POST",
            path="/api/runs",
            run_id="00000000-0000-0000-0000-000000000001",
        )
        await record_audit_event(
            session,
            context,
            AuditEventType.RUN_CREATED,
            AuditOutcome.SUCCESS,
            {"status": "pending", "X-Admin-Token": "must-not-persist"},
        )
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            sa.select(AuditEvent).where(
                AuditEvent.request_id == request_id,
                AuditEvent.event_type == AuditEventType.RUN_CREATED,
            )
        )
        event = result.scalar_one()

    assert event.method == "POST"
    assert event.path == "/api/runs"
    assert event.details == {"status": "pending", "X-Admin-Token": "[REDACTED]"}


@pytest.mark.asyncio
async def test_audit_event_rolls_back_with_business_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = f"request-{uuid.uuid4()}"
    with pytest.raises(RuntimeError, match="business failure"):
        async with session_factory() as session:
            await record_audit_event(
                session,
                AuditContext(request_id=request_id, method="POST", path="/api/runs"),
                AuditEventType.RUN_CREATED,
                AuditOutcome.SUCCESS,
                {"status": "pending"},
            )
            raise RuntimeError("business failure")

    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.request_id == request_id)
        )
        assert result.scalar_one() == 0
