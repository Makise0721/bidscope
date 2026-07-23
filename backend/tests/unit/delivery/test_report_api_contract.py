from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from bidscope.delivery.docx import ReportDelivery
from bidscope.delivery.reports import PersistedReport
from bidscope.domain.reports import Report
from bidscope.persistence.models import Report as ReportModel

RUN_ID = "11111111-1111-1111-1111-111111111111"
REPORT_ID = "22222222-2222-2222-2222-222222222222"


class _FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        return key

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class _CancellingObjectStore(_FakeObjectStore):
    def put_bytes(self, key: str, data: bytes) -> str:
        raise asyncio.CancelledError()


class _FakeSession:
    def __init__(self, row: ReportModel | None) -> None:
        self.row = row
        self.commit = AsyncMock()
        self.add = Mock()

    async def get(self, _model: object, _id: str) -> ReportModel | None:
        return self.row


class _SessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _sample_report() -> Report:
    return Report(
        run_id=RUN_ID,
        generated_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        query_conditions={"region": "Sichuan"},
    )


@pytest.mark.asyncio
async def test_export_attaches_docx_to_existing_typed_online_report() -> None:
    report = _sample_report()
    row = ReportModel(
        id=REPORT_ID,
        run_id=RUN_ID,
        export_key="online:" + RUN_ID,
        generated_at=report.generated_at,
    )
    session = _FakeSession(row)
    session_factory = Mock(return_value=_SessionContext(session))
    delivery = ReportDelivery(store=_FakeObjectStore(), session_factory=session_factory)

    record = await delivery.export_report(PersistedReport(REPORT_ID, report, None))

    assert record.report_id == REPORT_ID
    assert row.docx_object_key == record.object_key
    session.add.assert_not_called()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_does_not_convert_cancellation_to_delivery_error() -> None:
    report = _sample_report()
    row = ReportModel(
        id=REPORT_ID,
        run_id=RUN_ID,
        export_key="online:" + RUN_ID,
        generated_at=report.generated_at,
    )
    session_factory = Mock(return_value=_SessionContext(_FakeSession(row)))
    delivery = ReportDelivery(
        store=_CancellingObjectStore(), session_factory=session_factory
    )

    with pytest.raises(asyncio.CancelledError):
        await delivery.export_report(PersistedReport(REPORT_ID, report, None))
