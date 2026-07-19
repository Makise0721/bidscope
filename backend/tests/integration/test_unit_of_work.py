from datetime import datetime

import pytest_asyncio
import sqlalchemy as sa
from bidscope.persistence.models import CanonicalNotice, SourceNotice
from bidscope.persistence.unit_of_work import UnitOfWork
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def _count(session: AsyncSession) -> int:
    result = await session.execute(sa.select(sa.func.count()).select_from(SourceNotice))
    return result.scalar_one()


@pytest_asyncio.fixture
async def canonical_notice_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with UnitOfWork(session_factory) as uow:
        notice = CanonicalNotice()
        uow.session.add(notice)
    return notice.id


async def test_commit_persists_rows(
    session_factory: async_sessionmaker[AsyncSession],
    canonical_notice_id: str,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            SourceNotice(
                canonical_notice_id=canonical_notice_id,
                source="ccgp",
                external_id="ext-1",
                source_url="https://www.ccgp.gov.cn/a.htm",
                first_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                latest_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                content_hash="hash-1",
            )
        )

    async with session_factory() as session:
        assert await _count(session) == 1


async def test_rollback_on_exception_leaves_no_rows(
    session_factory: async_sessionmaker[AsyncSession],
    canonical_notice_id: str,
) -> None:
    try:
        async with UnitOfWork(session_factory) as uow:
            uow.session.add(
                SourceNotice(
                    canonical_notice_id=canonical_notice_id,
                    source="ccgp",
                    external_id="ext-rollback",
                    source_url="https://www.ccgp.gov.cn/b.htm",
                    first_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                    latest_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                    content_hash="hash-r",
                )
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    async with session_factory() as session:
        assert await _count(session) == 0


async def test_unique_constraint_violation_does_not_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
    canonical_notice_id: str,
) -> None:
    async with UnitOfWork(session_factory) as uow:
        uow.session.add(
            SourceNotice(
                canonical_notice_id=canonical_notice_id,
                source="ccgp",
                external_id="ext-dup",
                source_url="https://www.ccgp.gov.cn/c.htm",
                first_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                latest_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                content_hash="hash-d1",
            )
        )

    raised = False
    try:
        async with UnitOfWork(session_factory) as uow:
            uow.session.add(
                SourceNotice(
                    canonical_notice_id=canonical_notice_id,
                    source="ccgp",
                    external_id="ext-dup",
                    source_url="https://www.ccgp.gov.cn/c.htm",
                    first_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                    latest_seen_at=_ts("2026-07-18T00:00:00+00:00"),
                    content_hash="hash-d2",
                )
            )
    except IntegrityError:
        raised = True

    assert raised
    async with session_factory() as session:
        assert await _count(session) == 1
