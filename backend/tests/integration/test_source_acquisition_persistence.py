import asyncio
from datetime import UTC, datetime

import pytest
from bidscope.persistence.models import SourceAcquisitionRun, SourceSyncCursor
from bidscope.persistence.repositories import SourceAcquisitionRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_first_cursor_creation_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    watermark = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        first = await repository.get_or_create_source_sync_cursor(
            source="ccgp",
            cursor_value="",
            watermark_at=watermark,
        )
        await session.commit()

    async with session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        second = await repository.get_or_create_source_sync_cursor(
            source="ccgp",
            cursor_value="ignored-after-first-create",
            watermark_at=datetime(2026, 7, 30, 1, 0, tzinfo=UTC),
        )

    assert second.source == first.source == "ccgp"
    assert second.cursor_value == ""
    assert second.watermark_at == watermark


@pytest.mark.asyncio
async def test_successful_cursor_advancement_is_atomic_and_versioned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        cursor = await repository.get_or_create_source_sync_cursor(
            source="ccgp",
            cursor_value="page:1",
            watermark_at=datetime(2026, 7, 30, 0, 0, tzinfo=UTC),
        )
        advanced = await repository.advance_source_sync_cursor(
            source="ccgp",
            expected_version=cursor.version,
            cursor_before="page:1",
            cursor_after="page:2",
            watermark_at=datetime(2026, 7, 30, 1, 0, tzinfo=UTC),
            succeeded_at=datetime(2026, 7, 30, 1, 0, 1, tzinfo=UTC),
        )
        await session.commit()

    assert advanced is True

    async with session_factory() as session:
        stored = await session.get(SourceSyncCursor, "ccgp")

    assert stored is not None
    assert stored.cursor_value == "page:2"
    assert stored.watermark_at == datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
    assert stored.last_success_at == datetime(2026, 7, 30, 1, 0, 1, tzinfo=UTC)
    assert stored.consecutive_failures == 0
    assert stored.version == 1


@pytest.mark.asyncio
async def test_failed_acquisition_retains_prior_cursor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started_at = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
    finished_at = datetime(2026, 7, 30, 2, 0, 5, tzinfo=UTC)

    async with session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        await repository.get_or_create_source_sync_cursor(
            source="ccgp",
            cursor_value="page:7",
            watermark_at=started_at,
        )
        run = await repository.create_acquisition_run(
            source="ccgp",
            started_at=started_at,
            cursor_before="page:7",
        )
        await repository.finalize_acquisition_run(
            run.id,
            finished_at=finished_at,
            status="failed",
            failure_code="timeout",
            request_count=1,
        )
        await session.commit()

    async with session_factory() as session:
        cursor = await session.get(SourceSyncCursor, "ccgp")
        run = await session.get(SourceAcquisitionRun, run.id)

    assert cursor is not None
    assert cursor.cursor_value == "page:7"
    assert cursor.version == 1
    assert cursor.consecutive_failures == 1
    assert run is not None
    assert run.status == "failed"
    assert run.cursor_before == "page:7"
    assert run.cursor_after is None
    assert run.failure_code == "timeout"


@pytest.mark.asyncio
async def test_cursor_lock_excludes_concurrent_worker_until_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        await repository.get_or_create_source_sync_cursor(
            source="ccgp",
            cursor_value="page:1",
            watermark_at=datetime(2026, 7, 30, 0, 0, tzinfo=UTC),
        )
        await session.commit()

    first_session = session_factory()
    second_session = session_factory()
    try:
        first_repository = SourceAcquisitionRepository(first_session)
        second_repository = SourceAcquisitionRepository(second_session)
        await first_repository.get_source_sync_cursor_for_update("ccgp")

        second_lock = asyncio.create_task(
            second_repository.get_source_sync_cursor_for_update("ccgp")
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_lock), timeout=0.2)

        await first_session.commit()
        locked = await asyncio.wait_for(second_lock, timeout=5)
        assert locked is not None
        assert locked.cursor_value == "page:1"
    finally:
        await first_session.close()
        await second_session.close()
