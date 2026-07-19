"""Integration tests for idempotent, version-preserving snapshot import.

These tests run against the Compose test database (see ``conftest.py``) and
exercise the full import path: bundle integrity inspection, deterministic
object storage, and the transactional creation of snapshot bundles, source
notices, immutable notice versions, and evidence rows.

The importer must be idempotent (re-importing the same bundle returns the same
logical import and adds no rows) and version-aware (a changed content hash
creates a new immutable version while keeping one logical source notice).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.persistence.models import (
    NoticeEvidence,
    NoticeVersion,
    SnapshotBundle,
    SnapshotImport,
    SourceNotice,
)
from bidscope.snapshots.importer import SnapshotImportError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[3]

CCGP_BUNDLE = PROJECT_ROOT / "data/snapshots/ccgp/2026-07-18-central-open"
DEMO_BATCH_1 = PROJECT_ROOT / "data/demo/batch-1"
DEMO_BATCH_2 = PROJECT_ROOT / "data/demo/batch-2"


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


@pytest_asyncio.fixture(autouse=True)
async def clean_snapshot_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate snapshot tables for test isolation.

    ``_clean_tables`` only truncates ``source_notices`` + ``canonical_notices``;
    snapshot bundles/imports are not cleared there, so we truncate them here.
    Without this, rows (and the unique ``idempotency_key``) would accumulate
    across tests and corrupt row-count assertions.
    """
    async with session_factory() as session:
        await session.execute(
            sa.text("TRUNCATE snapshot_imports, snapshot_bundles CASCADE")
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The importer is imported lazily so that the RED phase fails with a clean
# ImportError (module missing) rather than a collection-time NameError.
# ---------------------------------------------------------------------------


@pytest.fixture
def ccgp_bundle() -> Path:
    return PROJECT_ROOT / "data/snapshots/ccgp/2026-07-18-central-open"


@pytest.fixture
def demo_batch_1() -> Path:
    return PROJECT_ROOT / "data/demo/batch-1"


@pytest.fixture
def demo_batch_2() -> Path:
    return PROJECT_ROOT / "data/demo/batch-2"


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
# Idempotency
# ---------------------------------------------------------------------------


async def test_reimport_is_idempotent(importer, ccgp_bundle, session_factory) -> None:
    first = await importer.import_bundle(ccgp_bundle)
    second = await importer.import_bundle(ccgp_bundle)

    assert first.id == second.id
    assert first.status == "success"

    async with session_factory() as session:
        assert await _count(session, SourceNotice) == 1
        assert await _count(session, NoticeVersion) == 1


async def test_reimport_does_not_duplicate_source_notice(
    importer, demo_batch_1, session_factory
) -> None:
    await importer.import_bundle(demo_batch_1)
    await importer.import_bundle(demo_batch_1)

    async with session_factory() as session:
        assert await _count(session, SourceNotice) == 12
        assert await _count(session, NoticeVersion) == 12


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


async def test_new_version_when_content_changes(
    importer, demo_batch_1, demo_batch_2, session_factory
) -> None:
    await importer.import_bundle(demo_batch_1)
    await importer.import_bundle(demo_batch_2)

    async with session_factory() as session:
        # demo-001/demo-002 changed -> +2 versions; demo-003/004 unchanged;
        # demo-013/demo-014 new -> +2 source notices and +2 versions.
        assert await _count(session, SourceNotice) == 14
        assert await _count(session, NoticeVersion) == 16


async def test_unchanged_content_does_not_create_version(
    importer, demo_batch_1, session_factory
) -> None:
    await importer.import_bundle(demo_batch_1)
    # Re-importing the same batch: demo-003/004 etc. unchanged -> no new versions.
    await importer.import_bundle(demo_batch_1)

    async with session_factory() as session:
        assert await _count(session, NoticeVersion) == 12


async def test_different_source_can_share_external_id(
    importer, tmp_path: Path, session_factory
) -> None:
    """Same external_id under two different sources yields two source notices."""
    bundle_a = tmp_path / "bundle-a"
    _write_demo_bundle(
        bundle_a,
        [{"id": "demo-shared", "url": "https://example.invalid/shared", "title": "A"}],
        "demo-shared-a",
    )
    # The importer keys source off the bundle's declared source, so to exercise
    # two sources we import the same logical record via two distinct adapters
    # by registering a second synthetic source. Simpler: assert the schema
    # allows it directly via the repository.
    from bidscope.persistence.repositories import SnapshotRepository

    async with session_factory() as session:
        repo = SnapshotRepository(session)
        a = await repo.get_or_create_source_notice(
            source="synthetic_demo",
            external_id="demo-shared",
            source_url="https://example.invalid/a",
            content_hash="h1",
            first_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
            latest_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        b = await repo.get_or_create_source_notice(
            source="ccgp",
            external_id="demo-shared",
            source_url="https://www.ccgp.gov.cn/a.htm",
            content_hash="h1",
            first_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
            latest_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        await session.commit()
        assert a.id != b.id
        assert await _count(session, SourceNotice) == 2


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_invalid_bundle_fails_before_transaction(
    importer, tmp_path: Path, session_factory
) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("not json", encoding="utf-8")

    with pytest.raises(SnapshotImportError):
        await importer.import_bundle(bad)

    async with session_factory() as session:
        assert await _count(session, SnapshotBundle) == 0
        assert await _count(session, SnapshotImport) == 0
        assert await _count(session, SourceNotice) == 0


async def test_mid_import_error_rolls_back(
    importer_cls, repository_cls, session_factory, object_store, clock, demo_batch_1
) -> None:
    """A database error mid-import must leave no partial records."""
    from bidscope.persistence.repositories import SnapshotRepository

    class FaultyRepository(SnapshotRepository):
        async def create_version(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("injected db fault")

    faulty_importer = importer_cls(
        session_factory=session_factory,
        repository_factory=FaultyRepository,
        object_store=object_store,
        clock=clock,
    )

    with pytest.raises(RuntimeError, match="injected db fault"):
        await faulty_importer.import_bundle(demo_batch_1)

    async with session_factory() as session:
        assert await _count(session, SnapshotBundle) == 0
        assert await _count(session, SourceNotice) == 0
        assert await _count(session, NoticeVersion) == 0


# ---------------------------------------------------------------------------
# Evidence + provenance
# ---------------------------------------------------------------------------


async def test_evidence_linked_to_correct_version(
    importer, demo_batch_1, session_factory
) -> None:
    result = await importer.import_bundle(demo_batch_1)
    assert result.status == "success"

    async with session_factory() as session:
        versions = (await session.execute(sa.select(NoticeVersion))).scalars().all()
        assert versions, "expected at least one notice version"
        for version in versions:
            evidence = (
                await session.execute(
                    sa.select(NoticeEvidence).where(
                        NoticeEvidence.notice_version_id == version.id
                    )
                )
            ).scalars().all()
            assert evidence, f"version {version.id} has no evidence rows"
            for ev in evidence:
                assert ev.notice_version_id == version.id


# ---------------------------------------------------------------------------
# Regression tests for findings surfaced in the batch-2 review
# ---------------------------------------------------------------------------


async def test_evidence_covers_each_parsed_field_with_datetime(
    importer, ccgp_bundle, session_factory
) -> None:
    """M1 regression: every parsed field — including publish_time and deadline —
    produces an evidence span. The count is deterministic and field-exact."""
    await importer.import_bundle(ccgp_bundle)

    async with session_factory() as session:
        (version,) = (await session.execute(sa.select(NoticeVersion))).scalars().all()
        evidence = (
            await session.execute(
                sa.select(NoticeEvidence).where(
                    NoticeEvidence.notice_version_id == version.id
                )
            )
        ).scalars().all()

    # CCGP parses: title, purchaser, region, publish_time, deadline, budget.
    # summary is absent in the fixture, so exactly six evidence spans are
    # expected — one per populated, material field (no raw_fields to add).
    assert len(evidence) == 6, f"expected 6 evidence spans, got {len(evidence)}"
    texts = {span.text for span in evidence}
    # ISO-8601 evidence for the two datetime fields is present.
    assert any("2026-07-18" in text for text in texts), "publish_time evidence missing"
    assert any("2026-08-10" in text for text in texts), "deadline evidence missing"


async def test_inspect_bundle_accepts_relative_path() -> None:
    """M2 regression: inspect_bundle must resolve relative paths internally so
    callers need not pass an already-resolved path."""
    from bidscope.snapshots.adapters import inspect_bundle

    relative = Path("data/demo/batch-1")
    absolute = relative.resolve()
    assert inspect_bundle(relative).valid == inspect_bundle(absolute).valid
    assert inspect_bundle(relative).valid is True


async def test_evidence_excludes_metadata_fields(
    importer, demo_batch_1, session_factory
) -> None:
    """M4 regression: raw_fields metadata (e.g. synthetic_channel) must not be
    written into the evidence table as if it were a business fact."""
    await importer.import_bundle(demo_batch_1)

    async with session_factory() as session:
        evidence = (await session.execute(sa.select(NoticeEvidence))).scalars().all()

    texts = {span.text for span in evidence}
    assert "channel_a" not in texts, "synthetic_channel leaked into evidence"
    assert "channel_b" not in texts, "synthetic_channel leaked into evidence"


async def test_concurrent_source_notice_is_deduplicated(session_factory) -> None:
    """M5 regression: concurrent get_or_create_source_notice calls for the same
    (source, external_id) must yield exactly one source notice, with any unique
    constraint collision handled via savepoint rollback — no unhandled error."""
    import asyncio

    from bidscope.persistence.repositories import SnapshotRepository

    async def create(session: AsyncSession) -> str:
        repo = SnapshotRepository(session)
        notice = await repo.get_or_create_source_notice(
            source="synthetic_demo",
            external_id="demo-race",
            source_url="https://example.invalid/race",
            content_hash="race-hash",
            first_seen_at=datetime(2026, 7, 1, tzinfo=UTC),
            latest_seen_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        await session.commit()
        return str(notice.id)

    async with session_factory() as first, session_factory() as second:
        results = await asyncio.gather(
            create(first), create(second), return_exceptions=True
        )

    # No unhandled error may propagate.
    assert not any(isinstance(r, Exception) for r in results), results
    # Whatever the race outcome, the logical external_id must not be duplicated.
    async with session_factory() as session:
        existing = await _count(
            session,
            sa.select(SourceNotice)
            .where(
                SourceNotice.source == "synthetic_demo",
                SourceNotice.external_id == "demo-race",
            )
            .subquery(),
        )
    assert existing == 1, f"expected exactly 1 source notice, got {existing}"


async def test_provenance_inconsistency_not_persisted(
    importer, tmp_path: Path, session_factory
) -> None:
    """A notice whose source/capture/host disagree must not be persisted.

    Provenance validation happens inside :class:`NormalizedNotice`, so the
    failure surfaces as a pydantic :class:`ValidationError` before any database
    write. The error is raised by the adapter's ``parse`` (before the write
    transaction opens), so no rows are ever created.
    """
    from pydantic import ValidationError

    bad = tmp_path / "bad-provenance"
    _write_demo_bundle(
        bad,
        [
            {
                "id": "demo-bad",
                # synthetic_demo requires example.invalid; ccgp host fails provenance.
                "url": "https://www.ccgp.gov.cn/demo-bad",
                "title": "bad",
            }
        ],
        "demo-bad-provenance",
    )

    with pytest.raises(ValidationError):
        await importer.import_bundle(bad)

    async with session_factory() as session:
        assert await _count(session, SourceNotice) == 0
        assert await _count(session, NoticeVersion) == 0


# ---------------------------------------------------------------------------
# Batch 2 semantics (new / changed / unchanged)
# ---------------------------------------------------------------------------


async def test_batch_two_new_changed_unchanged(
    importer, demo_batch_1, demo_batch_2, session_factory
) -> None:
    await importer.import_bundle(demo_batch_1)
    await importer.import_bundle(demo_batch_2)

    async with session_factory() as session:
        notices = (await session.execute(sa.select(SourceNotice))).scalars().all()
        by_key = {(n.source, n.external_id): n for n in notices}

        # New notices (demo-013, demo-014) created a source notice.
        assert ("synthetic_demo", "demo-013") in by_key
        assert ("synthetic_demo", "demo-014") in by_key

        # demo-001 changed content -> two versions, one source notice.
        demo_001 = by_key[("synthetic_demo", "demo-001")]
        versions = (
            await session.execute(
                sa.select(NoticeVersion).where(
                    NoticeVersion.source_notice_id == demo_001.id
                )
            )
        ).scalars().all()
        assert len(versions) == 2

        # demo-003 unchanged -> exactly one version.
        demo_003 = by_key[("synthetic_demo", "demo-003")]
        versions = (
            await session.execute(
                sa.select(NoticeVersion).where(
                    NoticeVersion.source_notice_id == demo_003.id
                )
            )
        ).scalars().all()
        assert len(versions) == 1
