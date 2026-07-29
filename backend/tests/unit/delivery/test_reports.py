"""Unit tests for report-delivery orchestration metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from bidscope.delivery.docx import DeliveryError, ExportRecord
from bidscope.delivery.objects import ObjectStore
from bidscope.delivery.reports import PersistedReport, ReportPersistence
from bidscope.observability import MetricsRegistry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _SuccessfulDelivery:
    async def export_report(self, persisted: PersistedReport) -> ExportRecord:
        _ = persisted
        return ExportRecord(
            export_key="docx-v1:report-1",
            object_key="reports/report-1.docx",
            report_id="report-1",
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


class _FailingDelivery:
    async def export_report(self, persisted: PersistedReport) -> ExportRecord:
        _ = persisted
        raise DeliveryError("object store unavailable")


def _persistence() -> ReportPersistence:
    """Build an exporter wrapper without invoking its I/O dependencies."""
    return ReportPersistence(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(ObjectStore, object()),
    )


@pytest.mark.asyncio
async def test_export_docx_records_success_duration_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MetricsRegistry()
    monkeypatch.setattr("bidscope.delivery.reports.METRICS_REGISTRY", registry)
    persistence = _persistence()
    persistence._delivery = _SuccessfulDelivery()  # type: ignore[assignment]

    result = await persistence.export_docx(cast(PersistedReport, object()))

    assert result.report_id == "report-1"
    assert (
        'bidscope_report_delivery_duration_seconds_count{outcome="success"} 1'
        in registry.render_prometheus()
    )


@pytest.mark.asyncio
async def test_export_docx_records_failed_duration_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MetricsRegistry()
    monkeypatch.setattr("bidscope.delivery.reports.METRICS_REGISTRY", registry)
    persistence = _persistence()
    persistence._delivery = _FailingDelivery()  # type: ignore[assignment]

    with pytest.raises(DeliveryError, match="object store unavailable"):
        await persistence.export_docx(cast(PersistedReport, object()))

    assert (
        'bidscope_report_delivery_duration_seconds_count{outcome="failed"} 1'
        in registry.render_prometheus()
    )
