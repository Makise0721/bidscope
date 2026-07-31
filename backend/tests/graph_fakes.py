"""Shared persistence stub for isolated graph tests."""

from __future__ import annotations

from bidscope.delivery.docx import ExportRecord
from bidscope.delivery.reports import PersistedReport


class FakeReportPersistence:
    """Captures validated reports without using a database or object store."""

    def __init__(self) -> None:
        self.persisted: list[PersistedReport] = []
        self.exports: list[PersistedReport] = []

    async def persist_online_report(
        self,
        report,
        evidence_by_hash,
        claim_verifications=(),
    ):  # type: ignore[no-untyped-def]
        _ = evidence_by_hash
        _ = claim_verifications
        persisted = PersistedReport(
            id=f"test-report-{len(self.persisted) + 1}",
            report=report,
            docx_object_key=None,
        )
        self.persisted.append(persisted)
        return persisted

    async def export_docx(self, persisted: PersistedReport) -> ExportRecord:
        self.exports.append(persisted)
        return ExportRecord(
            export_key=f"docx-v1:{persisted.id}",
            object_key=f"reports/{persisted.id}.docx",
            report_id=persisted.id,
            generated_at=persisted.report.generated_at,
        )
