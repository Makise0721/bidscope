"""Integration tests for idempotent DOCX report delivery.

These tests run against the Compose test database (see ``conftest.py``) and a
real :class:`~bidscope.delivery.objects.LocalObjectStore` to verify the full
export path: a typed :class:`~bidscope.domain.reports.Report` is rendered to
DOCX, the bytes are stored, and a logical export record is persisted. Exporting
the same report twice must yield the same export record and the same object key
without creating a second stored object or a second database row.

Only synthetic-demo data is used; no network access occurs.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import docx
import pytest
import sqlalchemy as sa
from bidscope.delivery.docx import ReportDelivery
from bidscope.delivery.objects import LocalObjectStore
from bidscope.domain.reports import (
    Report,
    ReportCitation,
    ReportClaim,
    ReportItem,
)
from bidscope.persistence.models import QueryRun
from bidscope.persistence.models import Report as ReportModel
from sqlalchemy.ext.asyncio import AsyncSession

REPORT_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
async def _seed_query_run(
    session_factory: sa.orm.sessionmaker, _clean_tables: None,
) -> None:
    """Insert a QueryRun row so the reports.run_id foreign key is satisfiable.

    ``reports.run_id`` references ``query_runs.id``; without a parent row the
    export would trip a foreign-key violation. ``_clean_tables`` truncates
    ``query_runs`` before each test, so we re-seed here, depending on it to
    guarantee ordering.
    """
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=REPORT_ID,
                run_key=REPORT_ID,
                status="completed",
                user_request="test",
            )
        )
        await session.commit()


def _sample_report() -> Report:
    """Build a representative synthetic-demo report for delivery tests."""
    return Report(
        run_id=REPORT_ID,
        generated_at=datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC),
        query_conditions={"region": "四川", "budget": "≥500万"},
        freshness_window=None,
        source_availability=["ccgp"],
        completeness_warning="部分数据源暂不可用，结果可能不完整。",
        items=[
            ReportItem(
                notice_id="demo-001",
                title="四川智算中心服务器采购招标公告",
                known_fields={"source_url": "https://example.invalid/demo/001"},
                unknown_fields=["deadline"],
                relevance_reason=None,
                risk_note=None,
                citations=[ReportCitation(evidence_id="ev-001", label="预算金额证据")],
                claims=[
                    ReportClaim(text="预算800万元。", citation_ids=["ev-001"]),
                ],
            ),
        ],
    )


def _make_delivery(session_factory: sa.orm.sessionmaker, tmp_path) -> ReportDelivery:
    store = LocalObjectStore(root=tmp_path / "objects")
    return ReportDelivery(store=store, session_factory=session_factory)


async def _count_exports(session: AsyncSession, export_key: str) -> int:
    result = await session.execute(
        sa.select(sa.func.count()).select_from(ReportModel).where(
            ReportModel.export_key == export_key
        )
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_export_stores_docx_and_persists_record(
    session_factory: sa.orm.sessionmaker,
    tmp_path,
) -> None:
    """The first export renders, stores bytes, and persists one record."""
    delivery = _make_delivery(session_factory, tmp_path)
    report = _sample_report()

    record = await delivery.export_report(report)

    # The object key points to a stored, readable DOCX.
    assert delivery.store.exists(record.object_key)
    data = delivery.store.get_bytes(record.object_key)
    assert isinstance(docx.Document(io.BytesIO(data)), docx.document.Document)

    # Exactly one export record was persisted for this key.
    async with session_factory() as session:
        assert await _count_exports(session, record.export_key) == 1


@pytest.mark.asyncio
async def test_export_is_idempotent(
    session_factory: sa.orm.sessionmaker,
    tmp_path,
) -> None:
    """Exporting the same report twice yields one record and one object key."""
    delivery = _make_delivery(session_factory, tmp_path)
    report = _sample_report()

    first = await delivery.export_report(report)
    second = await delivery.export_report(report)

    # Same logical export, same stored object.
    assert first.export_key == second.export_key
    assert first.object_key == second.object_key

    # Still only one export record and one stored object.
    async with session_factory() as session:
        assert await _count_exports(session, first.export_key) == 1
    assert delivery.store.exists(first.object_key)
