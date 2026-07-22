from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from bidscope.delivery.docx import ReportDelivery
from bidscope.domain.reports import Report
from bidscope.persistence.models import Report as ReportModel

RUN_ID = "11111111-1111-1111-1111-111111111111"


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


class _ScalarResult:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.execute = AsyncMock(return_value=_ScalarResult())
        self.commit = AsyncMock()

    def add(self, row: Any) -> None:
        self.added.append(row)


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
async def test_export_persists_typed_report_run_id() -> None:
    report = _sample_report()
    session = _FakeSession()
    session_factory = Mock(return_value=_SessionContext(session))
    delivery = ReportDelivery(
        store=_FakeObjectStore(),
        session_factory=session_factory,
    )

    await delivery.export_report(report)

    assert len(session.added) == 1
    persisted = session.added[0]
    assert isinstance(persisted, ReportModel)
    assert persisted.run_id == report.run_id
