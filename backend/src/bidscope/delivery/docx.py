"""DOCX rendering and idempotent storage for typed reports.

The renderer converts a :class:`~bidscope.domain.reports.Report` into a DOCX
byte stream using :mod:`python-docx`. It is a pure function: it touches no
database, object store, or network, and it never re-prompts a model.

:class:`ReportDelivery` persists the rendered bytes to an
:class:`~bidscope.delivery.objects.ObjectStore` and keeps a logical export
record so that exporting the same report twice produces a single stored object
and a single database row. The idempotency key is derived from the report's run
identifier plus a renderer version, so a renderer change produces a new export
while repeated exports of the same report collapse onto one record.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from bidscope.delivery.objects import ObjectStore
from bidscope.domain.reports import Report
from bidscope.persistence.models import Report as ReportModel
from docx import Document
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Bumped when the rendered output format changes. Part of the idempotency key
#: so a format change yields a fresh export rather than reusing stale bytes.
RENDERER_VERSION = "docx-v1"


@dataclass(frozen=True)
class ExportRecord:
    """A logical record of one idempotent DOCX export."""

    export_key: str
    object_key: str
    report_id: str
    generated_at: datetime


def _export_key(report: Report) -> str:
    """Derive the idempotent export key from the report ID + renderer version."""
    return f"{RENDERER_VERSION}:{report.run_id}"


def _sanitize_filename(name: str) -> str:
    """Strip characters unsafe inside an object key or DOCX filename."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    sanitized = sanitized.strip(".")
    return sanitized or "report"


def _object_key(report: Report) -> str:
    """Build the deterministic, sanitized object key for a report's DOCX."""
    safe = _sanitize_filename(report.run_id)
    return f"reports/{safe}/bidscope-{safe}.docx"


def render_report(report: Report) -> bytes:
    """Render a typed report to DOCX bytes.

    Pure function: no storage, no network. The output contains the query
    conditions, each item title, unknown-field markers, source URLs, evidence
    labels, and the completeness warning (when present).
    """
    document = Document()
    document.add_heading("BidScope Report", level=0)

    document.add_paragraph(f"Generated at: {report.generated_at.isoformat()}")
    if report.freshness_window:
        document.add_paragraph(f"Freshness window: {report.freshness_window}")

    # --- Query conditions -------------------------------------------------
    document.add_heading("Query Conditions", level=1)
    conditions_table = document.add_table(rows=1, cols=2)
    hdr = conditions_table.rows[0].cells
    hdr[0].text = "Field"
    hdr[1].text = "Value"
    for key, value in report.query_conditions.items():
        row = conditions_table.add_row().cells
        row[0].text = key
        row[1].text = str(value)

    # --- Source availability ----------------------------------------------
    if report.source_availability:
        document.add_heading("Source Availability", level=1)
        for source in report.source_availability:
            document.add_paragraph(source)

    # --- Completeness warning ---------------------------------------------
    if report.completeness_warning:
        document.add_heading("Completeness Warning", level=1)
        document.add_paragraph(report.completeness_warning)

    # --- Items / opportunities --------------------------------------------
    document.add_heading("Opportunities", level=1)
    for idx, item in enumerate(report.items, start=1):
        document.add_heading(f"{idx}. {item.title}", level=2)

        if item.known_fields:
            document.add_paragraph("Known fields:")
            kf_table = document.add_table(rows=1, cols=2)
            kf_hdr = kf_table.rows[0].cells
            kf_hdr[0].text = "Field"
            kf_hdr[1].text = "Value"
            for key, value in item.known_fields.items():
                row = kf_table.add_row().cells
                row[0].text = key
                row[1].text = str(value)

        if item.unknown_fields:
            document.add_paragraph(
                "未知字段 (unknown fields): " + ", ".join(item.unknown_fields)
            )

        if item.relevance_reason:
            document.add_paragraph(f"Relevance: {item.relevance_reason}")
        if item.risk_note:
            document.add_paragraph(f"Risk: {item.risk_note}")

        if item.citations:
            document.add_paragraph("Evidence:")
            for cidx, citation in enumerate(item.citations, start=1):
                label = citation.label or citation.evidence_id
                document.add_paragraph(f"  [{cidx}] {label}")

        if item.claims:
            document.add_paragraph("Claims:")
            for claim in item.claims:
                document.add_paragraph(f"  - {claim.text}")

    # --- Appendix ---------------------------------------------------------
    document.add_heading("Appendix", level=1)
    document.add_paragraph(f"Renderer version: {RENDERER_VERSION}")
    document.add_paragraph(f"Report run ID: {report.run_id}")
    document.add_paragraph(
        "This document was generated automatically and reflects the "
        "evidence available at generation time."
    )

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class ReportDelivery:
    """Persist rendered DOCX reports to an ObjectStore, idempotently."""

    def __init__(
        self,
        store: ObjectStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.store = store
        self.session_factory = session_factory

    async def export_report(self, report: Report) -> ExportRecord:
        """Render, store, and record a DOCX export. Idempotent per report."""
        export_key = _export_key(report)
        async with self.session_factory() as session:
            existing = await self._find_export(session, export_key)
            if existing is not None:
                # A persisted export always carries its object key; the column is
                # nullable only to accommodate rows created by other paths.
                assert existing.docx_object_key is not None
                return ExportRecord(
                    export_key=existing.export_key,
                    object_key=existing.docx_object_key,
                    report_id=report.run_id,
                    generated_at=existing.generated_at,
                )

            object_key = _object_key(report)
            data = render_report(report)
            self.store.put_bytes(object_key, data)

            row = ReportModel(
                run_id=report.run_id,
                export_key=export_key,
                conditions=report.query_conditions,
                freshness_window=report.freshness_window,
                completeness_warning=report.completeness_warning,
                generated_at=report.generated_at,
                docx_object_key=object_key,
            )
            session.add(row)
            await session.commit()

        return ExportRecord(
            export_key=export_key,
            object_key=object_key,
            report_id=report.run_id,
            generated_at=report.generated_at,
        )

    async def _find_export(
        self, session: AsyncSession, export_key: str
    ) -> ReportModel | None:
        result = await session.execute(
            sa.select(ReportModel).where(ReportModel.export_key == export_key)
        )
        return result.scalar_one_or_none()
