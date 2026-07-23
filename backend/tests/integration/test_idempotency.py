"""Integration tests for idempotency guarantees (Task 19, RED phase).

Five idempotency surfaces are pinned here:

1. Snapshot re-import returns the same logical import and adds no rows.
2. ``_derive_idempotency_key`` is deterministic for identical content.
3. Object storage is content-addressed — re-import writes the same key.
4. DOCX export collapses onto a single row per report.
5. Graph execution deduplicates already-persisted node events.

Each test asserts the *desired* end state. Where the guard already exists the
test passes; where it is missing it fails, exposing the gap.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.delivery.objects import LocalObjectStore
from bidscope.domain.reports import Report
from graph_fakes import FakeReportPersistence
from bidscope.persistence.models import (
    NoticeVersion,
    QueryRun,
    RunEvent,
    SnapshotImport,
    SourceNotice,
)
from bidscope.persistence.models import (
    Report as ReportModel,
)
from bidscope.snapshots.importer import SnapshotImporter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
async def clean_idempotency_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate snapshot + report + run-event + query-run tables for isolation."""
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "TRUNCATE snapshot_imports, snapshot_bundles, reports, "
                "run_events, query_runs CASCADE"
            )
        )
        await session.commit()


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
def importer(session_factory, repository_cls, object_store, clock):
    return SnapshotImporter(
        session_factory=session_factory,
        repository_factory=repository_cls,
        object_store=object_store,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 1. Snapshot re-import is idempotent
# ---------------------------------------------------------------------------


async def test_snapshot_reimport_returns_same_import(
    importer, tmp_path: Path, session_factory
) -> None:
    """Re-importing the same bundle returns the same import and adds no rows.

    The importer derives a deterministic idempotency key from the bundle's
    content, so a second ``import_bundle`` call short-circuits on the existing
    successful record rather than creating a duplicate.
    """
    bundle = tmp_path / "idem"
    _write_demo_bundle(
        bundle,
        [
            {"id": "demo-idem-1", "url": "https://example.invalid/demo-idem-1", "title": "A"},
            {"id": "demo-idem-2", "url": "https://example.invalid/demo-idem-2", "title": "B"},
        ],
        "demo-idem-bundle",
    )

    first = await importer.import_bundle(bundle)
    second = await importer.import_bundle(bundle)

    assert first.id == second.id
    assert first.status == "success"
    assert second.status == "success"

    async with session_factory() as session:
        assert await _count(session, SnapshotImport) == 1
        assert await _count(session, SourceNotice) == 2
        assert await _count(session, NoticeVersion) == 2


# ---------------------------------------------------------------------------
# 2. Idempotency key determinism
# ---------------------------------------------------------------------------


async def test_idempotency_key_is_deterministic(
    importer, tmp_path: Path
) -> None:
    """``_derive_idempotency_key`` produces the same key for identical content.

    The key is a pure function of ``bundle_id`` + sorted notice content hashes,
    so two parses of the same bundle yield byte-identical keys regardless of
    call order or process.
    """
    bundle = tmp_path / "det"
    _write_demo_bundle(
        bundle,
        [{"id": "demo-det-1", "url": "https://example.invalid/demo-det-1", "title": "A"}],
        "demo-det-bundle",
    )

    manifest, notices = _parse_for_key(importer, bundle)
    key_a = importer._derive_idempotency_key(manifest.bundle_id, notices)
    key_b = importer._derive_idempotency_key(manifest.bundle_id, notices)

    assert key_a == key_b
    assert isinstance(key_a, str) and len(key_a) == 64  # SHA-256 hex


def _parse_for_key(
    importer: SnapshotImporter, bundle: Path
) -> tuple[Any, list[Any]]:
    """Parse a bundle the way ``import_bundle`` does, returning (manifest, notices)."""
    from bidscope.snapshots import _parse

    inspection = importer.import_inspect(bundle)
    assert inspection.valid
    manifest = _parse.load_manifest(bundle)
    notices = importer._parse(bundle)
    return manifest, notices


# ---------------------------------------------------------------------------
# 3. Content-addressed object storage
# ---------------------------------------------------------------------------


async def test_object_storage_is_content_addressed(
    importer, tmp_path: Path
) -> None:
    """Re-import stores identical bytes under the same key — no duplicates.

    Object keys are derived from each notice's content hash
    (``snapshots/{bundle_id}/{content_hash}``), so a second import overwrites
    the same path rather than creating a new object. The on-disk file count is
    unchanged after re-import.
    """
    bundle = tmp_path / "ca"
    _write_demo_bundle(
        bundle,
        [
            {"id": "demo-ca-1", "url": "https://example.invalid/demo-ca-1", "title": "A"},
            {"id": "demo-ca-2", "url": "https://example.invalid/demo-ca-2", "title": "B"},
        ],
        "demo-ca-bundle",
    )

    await importer.import_bundle(bundle)
    files_after_first = count_store_files(importer.object_store.root)

    await importer.import_bundle(bundle)
    files_after_second = count_store_files(importer.object_store.root)

    assert files_after_second == files_after_first
    assert files_after_first == 2  # one object per notice


def count_store_files(root: Path) -> int:
    """Count regular files recursively under an object store root."""
    return sum(1 for f in root.rglob("*") if f.is_file())


# ---------------------------------------------------------------------------
# 4. DOCX export idempotency
# ---------------------------------------------------------------------------


async def test_docx_export_is_idempotent(
    session_factory, tmp_path: Path
) -> None:
    """Exporting the same report twice produces a single stored object and row.

    The export key (``"{renderer_version}:{run_id}"``) is derived from the
    report's run identifier, so a second ``export_report`` finds the existing
    row and returns it without re-rendering or inserting a duplicate.
    """
    from bidscope.delivery.reports import ReportPersistence

    # ReportModel.run_id is a FK to query_runs.id, so back it with a real row.
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id, run_key=run_id, status="pending",
            user_request="四川服务器", checkpoint_thread_id=run_id,
        ))
        await session.commit()

    # Use an isolated store root so the file count is unambiguous.
    store = tmp_path / "docx-objects"
    store.mkdir()
    persistence = ReportPersistence(
        store=LocalObjectStore(store),
        session_factory=session_factory,
    )

    report = _demo_report(run_id=run_id)
    persisted = await persistence.persist_online_report(report, {})

    first = await persistence.export_docx(persisted)
    second = await persistence.export_docx(persisted)

    assert first.export_key == second.export_key
    assert first.object_key == second.object_key

    async with session_factory() as session:
        rows = await _count(session, ReportModel)
    assert rows == 1, f"expected 1 report row after double export, got {rows}"


def _demo_report(run_id: str) -> Report:
    return Report(
        run_id=run_id,
        generated_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        query_conditions={"topics": "服务器", "regions": "四川"},
    )


# ---------------------------------------------------------------------------
# 5. Graph event deduplication
# ---------------------------------------------------------------------------


async def test_graph_event_deduplication(
    session_factory,
) -> None:
    """Re-executing an already-completed run persists no duplicate events.

    :func:`~bidscope.graph.executor.execute` reads the persisted event count
    before streaming the graph and only appends events beyond that point, so a
    second ``execute`` call against the same ``run_id`` writes nothing.
    """
    from bidscope.clock import FixedClock
    from bidscope.graph.builder import GraphDeps, build_graph
    from bidscope.graph.executor import create_run, execute
    from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
    from langgraph.checkpoint.memory import InMemorySaver

    class _FakeSearcher:
        async def search(self, query, filters=None):  # type: ignore[no-untyped-def]
            from bidscope.retrieval.search import RetrievalResult

            return RetrievalResult(
                query=query, candidates=[], degraded_modes=[], filters_applied={},
            )

    def _views(ids):  # type: ignore[no-untyped-def]
        return {}

    deps = GraphDeps(
        intent_model=FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=_FakeSearcher(),  # type: ignore[arg-type]
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        load_notice_views=_views,
        report_persistence=FakeReportPersistence(),
    )
    graph = build_graph(deps, checkpointer=InMemorySaver())

    run_id, created = await create_run(
        "四川服务器招标", session_factory=session_factory,
    )
    assert created is True

    input_data = {"user_request": "四川服务器招标"}
    await execute(graph, run_id, input_data, session_factory=session_factory)
    async with session_factory() as session:
        after_first = await _count(session, RunEvent)

    await execute(graph, run_id, input_data, session_factory=session_factory)
    async with session_factory() as session:
        after_second = await _count(session, RunEvent)

    assert after_first > 0, "expected events from the first execute"
    assert after_second == after_first, (
        f"gap: second execute duplicated events ({after_first} -> {after_second})"
    )
