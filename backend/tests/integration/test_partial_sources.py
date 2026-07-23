"""Integration tests for partial-source handling (Task 19, RED phase).

These tests document the behaviour required when a snapshot source is only
partially available: one stale source alongside a valid one, a bundle mixing
parseable and unparseable records, the ``completeness_warning`` field on
partial reports, and the bounded error-union contract from design section 9.

Each test asserts the *desired* end state. Where a guard already exists the
test passes; where the guard is missing it fails, exposing the gap.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.domain.runs import SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.llm.fake import FakeReportModel
from bidscope.llm.types import ReportDraft, VerifiedOpportunity
from bidscope.persistence.models import (
    NoticeVersion,
    SnapshotBundle,
    SourceNotice,
)
from bidscope.snapshots._parse import ParseDrift
from bidscope.snapshots.importer import SnapshotImportError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEMO_BATCH_1 = PROJECT_ROOT / "data/demo/batch-1"


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(sa.select(sa.func.count()).select_from(model))
    return result.scalar_one()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_demo_bundle(path: Path, notices: list[dict[str, Any]], bundle_id: str) -> None:
    """Write a minimal synthetic-demo bundle (manifest + notices.json) to ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    notices_json = json.dumps({"notices": notices}, ensure_ascii=False, indent=2)
    (path / "notices.json").write_text(notices_json, encoding="utf-8")

    # Hash the ACTUAL written bytes: on Windows write_text translates "\n" to
    # "\r\n", so the on-disk bytes differ from the in-memory string encoding.
    files = {"notices.json": _sha256((path / "notices.json").read_bytes())}
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "source": "synthetic_demo",
        "capture_kind": "synthetic_demo",
        "source_urls": [f"https://example.invalid/{bundle_id}"],
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "retrieval_outcome": "n/a",
        "parser_version": "demo-v1",
        "files": files,
    }
    (path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_raw_bundle(path: Path, notices_text: str, bundle_id: str) -> None:
    """Write a bundle whose ``notices.json`` is an arbitrary string.

    The manifest hash is computed from the on-disk bytes, so integrity
    inspection passes even when the payload is not a valid notices document.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / "notices.json").write_text(notices_text, encoding="utf-8")
    files = {"notices.json": _sha256((path / "notices.json").read_bytes())}
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "source": "synthetic_demo",
        "capture_kind": "synthetic_demo",
        "source_urls": [f"https://example.invalid/{bundle_id}"],
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "retrieval_outcome": "n/a",
        "parser_version": "demo-v1",
        "files": files,
    }
    (path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


@pytest_asyncio.fixture(autouse=True)
async def clean_snapshot_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate snapshot tables for test isolation."""
    async with session_factory() as session:
        await session.execute(
            sa.text("TRUNCATE snapshot_imports, snapshot_bundles CASCADE")
        )
        await session.commit()


@pytest.fixture
def importer_cls():
    from bidscope.snapshots.importer import SnapshotImporter

    return SnapshotImporter


@pytest.fixture
def repository_cls():
    from bidscope.persistence.repositories import SnapshotRepository

    return SnapshotRepository


@pytest.fixture
def object_store(tmp_path: Path):
    from bidscope.delivery.objects import LocalObjectStore

    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def clock():
    from bidscope.clock import FixedClock

    return FixedClock(datetime(2026, 7, 18, 6, 0, 0, tzinfo=UTC))


@pytest.fixture
def importer(importer_cls, repository_cls, session_factory, object_store, clock):
    return importer_cls(
        session_factory=session_factory,
        repository_factory=repository_cls,
        object_store=object_store,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# One stale + one valid source
# ---------------------------------------------------------------------------


async def test_stale_source_does_not_corrupt_valid_import(
    importer, tmp_path: Path, session_factory
) -> None:
    """A source that fails to parse must not disturb an already-imported source.

    The valid bundle imports cleanly; the stale bundle (whose ``notices.json``
    is not a documents array) raises ``ParseDrift``. The valid source's
    records remain intact and the stale source contributes nothing.
    """
    valid = tmp_path / "valid"
    _write_demo_bundle(
        valid,
        [
            {"id": "demo-valid-1", "url": "https://example.invalid/demo-valid-1", "title": "A"},
            {"id": "demo-valid-2", "url": "https://example.invalid/demo-valid-2", "title": "B"},
        ],
        "demo-valid",
    )

    stale = tmp_path / "stale"
    # Not a list — the demo adapter raises ParseDrift for this shape.
    _write_raw_bundle(stale, '{"notices": "not-a-list"}', "demo-stale")

    result = await importer.import_bundle(valid)
    assert result.status == "success"

    async with session_factory() as session:
        assert await _count(session, SourceNotice) == 2
        assert await _count(session, NoticeVersion) == 2

    with pytest.raises(ParseDrift):
        await importer.import_bundle(stale)

    # The stale source added nothing; the valid source is untouched.
    async with session_factory() as session:
        assert await _count(session, SourceNotice) == 2
        assert await _count(session, NoticeVersion) == 2
        assert await _count(session, SnapshotBundle) == 1


# ---------------------------------------------------------------------------
# Parse-invalid + valid source within one bundle
# ---------------------------------------------------------------------------


async def test_parse_invalid_record_does_not_block_valid_records(
    importer, tmp_path: Path, session_factory
) -> None:
    """Desired: a bundle mixing parseable and unparseable records imports the
    parseable ones and rejects only the unparseable record.

    The unparseable record must raise ParseDrift BEFORE any database write, so
    it is never persisted. The parseable record must still be imported — that
    partial-source resilience is the gap exposed here.
    """
    mixed = tmp_path / "mixed"
    _write_demo_bundle(
        mixed,
        [
            {"id": "demo-ok", "url": "https://example.invalid/demo-ok", "title": "OK"},
            # Empty URL → HttpUrl("") fails pydantic validation during parse.
            {"id": "demo-bad", "url": "", "title": "bad"},
        ],
        "demo-mixed",
    )

    # The bad record is rejected before the write transaction opens. The exact
    # surface (raise vs. recorded error) is unspecified, so we only assert the
    # end state: the good record must survive.
    with contextlib.suppress(ParseDrift, ValidationError, SnapshotImportError):
        await importer.import_bundle(mixed)

    async with session_factory() as session:
        imported = await _count(session, SourceNotice)

    # RED gap: current all-or-nothing parse fails the whole bundle, so the
    # valid record is lost. Desired behaviour imports it.
    assert imported >= 1, (
        "gap: partial import not supported — the valid record was not imported"
    )


# ---------------------------------------------------------------------------
# Partial report completeness warning
# ---------------------------------------------------------------------------


def test_report_draft_exposes_completeness_warning_field() -> None:
    """A synthesised report draft must carry a ``completeness_warning`` field.

    When some sources are unavailable the report is still produced but flagged
    via ``completeness_warning``. The field is optional (``None`` when all
    sources are present) so a fully-populated draft sets it to ``None``.
    """
    verified = VerifiedOpportunity(notice_id="demo-001", title="demo", evidence=())
    draft = FakeReportModel().synthesize(verified)

    assert isinstance(draft, ReportDraft)
    assert "completeness_warning" in ReportDraft.model_fields
    # No unavailable sources in this synthetic case → no warning.
    assert draft.completeness_warning is None


def test_report_domain_model_exposes_completeness_warning() -> None:
    """The persisted :class:`~bidscope.domain.reports.Report` also carries the flag."""
    from bidscope.domain.reports import Report

    assert "completeness_warning" in Report.model_fields
    report = Report(
        run_id="run-1",
        generated_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        query_conditions={},
    )
    assert report.completeness_warning is None


# ---------------------------------------------------------------------------
# Bounded error union (design section 9)
# ---------------------------------------------------------------------------


def test_serializable_error_only_accepts_bounded_codes() -> None:
    """``SerializableError.code`` is a ``BidScopeErrorCode`` — nothing else.

    Pydantic rejects any value outside the bounded enum, so a delivery or
    model error can never surface as an arbitrary string.
    """
    err = SerializableError(code=BidScopeErrorCode.DELIVERY_ERROR, message="boom")
    assert err.code is BidScopeErrorCode.DELIVERY_ERROR

    with pytest.raises(ValidationError):
        SerializableError(code="not_a_real_code", message="boom")


def test_error_serializes_to_bounded_code() -> None:
    """A serialized error's ``code`` is always one of the design-section-9 values."""
    err = SerializableError(code=BidScopeErrorCode.PARSE_DRIFT, message="drift")
    dumped = err.model_dump()

    bounded = {c.value for c in BidScopeErrorCode}
    assert dumped["code"] in bounded
    assert dumped["code"] == "parse_drift"
