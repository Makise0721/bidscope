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

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.api.dependencies import RunService
from bidscope.config import get_settings
from bidscope.delivery.objects import LocalObjectStore
from bidscope.domain.reports import Report
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
from graph_fakes import FakeReportPersistence
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


async def test_idempotency_key_is_deterministic(importer, tmp_path: Path) -> None:
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


def _parse_for_key(importer: SnapshotImporter, bundle: Path) -> tuple[Any, list[Any]]:
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


async def test_object_storage_is_content_addressed(importer, tmp_path: Path) -> None:
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


async def test_docx_export_is_idempotent(session_factory, tmp_path: Path) -> None:
    """Exporting the same report twice produces a single stored object and row.

    The export key (``"{renderer_version}:{run_id}"``) is derived from the
    report's run identifier, so a second ``export_report`` finds the existing
    row and returns it without re-rendering or inserting a duplicate.
    """
    from bidscope.delivery.reports import ReportPersistence

    # ReportModel.run_id is a FK to query_runs.id, so back it with a real row.
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key=run_id,
                status="pending",
                user_request="四川服务器",
                checkpoint_thread_id=run_id,
            )
        )
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
    """A terminal checkpoint is idempotent even while its row remains pending.

    :func:`~bidscope.graph.executor.execute` reads the checkpoint state before
    streaming the graph and returns it directly when it is terminal, regardless
    of the relational row status.
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
                query=query,
                candidates=[],
                degraded_modes=[],
                filters_applied={},
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
        "四川服务器招标",
        session_factory=session_factory,
    )
    assert created is True

    input_data = {"user_request": "四川服务器招标"}
    await execute(graph, run_id, input_data, session_factory=session_factory)
    async with session_factory() as session:
        after_first = await _count(session, RunEvent)
        run = await session.get(QueryRun, run_id)

    assert run is not None
    assert run.status == "pending"
    await execute(graph, run_id, input_data, session_factory=session_factory)
    async with session_factory() as session:
        after_second = await _count(session, RunEvent)

    assert after_first > 0, "expected events from the first execute"
    assert after_second == after_first, (
        f"gap: second execute duplicated events ({after_first} -> {after_second})"
    )


async def test_force_fresh_retry_resets_checkpoint_state_but_appends_events(
    session_factory,
) -> None:
    """Fresh retry keeps the thread and old events while discarding old state."""
    from bidscope.clock import FixedClock
    from bidscope.domain.runs import SerializableError
    from bidscope.graph.builder import GraphDeps, build_graph
    from bidscope.graph.executor import create_run, execute
    from bidscope.graph.state import DuplicateGroup
    from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
    from bidscope.llm.types import ModelUsage, VerifiedOpportunity
    from langgraph.checkpoint.memory import InMemorySaver

    class _FakeSearcher:
        async def search(self, query, filters=None):  # type: ignore[no-untyped-def]
            from bidscope.retrieval.search import RetrievalResult

            return RetrievalResult(
                query=query,
                candidates=[],
                degraded_modes=[],
                filters_applied={},
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
    checkpointer = InMemorySaver()
    graph = build_graph(deps, checkpointer=checkpointer)
    run_id, created = await create_run("四川服务器招标", session_factory=session_factory)
    assert created is True
    thread_id = "retry-thread"
    config = {"configurable": {"thread_id": thread_id}}
    old_event = {
        "node": "old_node",
        "event": "old_event",
        "status": "error",
        "timestamp": "2026-07-18T09:00:00+00:00",
    }
    old_error = SerializableError(
        code="graph_node_error", message="old error", details={}
    )
    old_opportunity = VerifiedOpportunity(notice_id="old-opportunity", title="old")
    old_usage = ModelUsage(
        model="old-model",
        prompt_tokens=9,
        completion_tokens=9,
        latency_ms=9.0,
        pricing_snapshot="old",
    )
    old_group = DuplicateGroup(
        representative_id="old-representative",
        member_ids=("old-member",),
        decision="exact",
    )
    await execute(
        graph,
        run_id,
        {"user_request": "old request"},
        session_factory=session_factory,
        checkpoint_thread_id=thread_id,
    )
    await graph.aupdate_state(
        config,
        {
            "candidate_notice_ids": ["old-candidate"],
            "duplicate_groups": [old_group],
            "errors": [old_error],
            "verified_opportunities": [old_opportunity],
            "node_events": [old_event],
            "token_usage": [old_usage],
            "retry_count": 7,
            "degraded_modes": ["old-mode"],
        },
    )
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "retryable"
        run.checkpoint_thread_id = thread_id
        old_event_seq = await session.scalar(
            sa.select(sa.func.count()).where(RunEvent.query_run_id == run_id)
        )
        assert old_event_seq is not None
        session.add(RunEvent(
            query_run_id=run_id,
            seq=old_event_seq,
            timestamp=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
            node="old_node",
            event="old_event",
            status="error",
        ))
        await session.commit()

    service = RunService(
        session_factory=session_factory,
        graph=graph,
        object_store=object(),
        settings=get_settings(),
    )
    result = await service.retry(run_id)

    assert result["status"] == "completed", result
    assert result["candidate_notice_ids"] == []
    assert result["duplicate_groups"] == []
    assert result["errors"] == []
    assert result["verified_opportunities"] == []
    assert result["token_usage"]
    assert all(usage != old_usage for usage in result["token_usage"])
    assert result.get("retry_count", 0) == 0
    assert result["degraded_modes"] == []
    assert all(event["event"] != "old_event" for event in result["node_events"])
    state = await graph.aget_state(config)
    assert state.values["candidate_notice_ids"] == []
    assert state.values["duplicate_groups"] == []
    assert state.values["errors"] == []
    assert state.values["verified_opportunities"] == []
    assert state.values["token_usage"]
    assert all(usage != old_usage for usage in state.values["token_usage"])
    assert state.values.get("retry_count", 0) == 0
    assert state.values["degraded_modes"] == []
    async with session_factory() as session:
        stored_run = await session.get(QueryRun, run_id)
        assert stored_run is not None
        assert stored_run.checkpoint_thread_id == thread_id
        events = await session.scalars(
            sa.select(RunEvent).where(RunEvent.query_run_id == run_id).order_by(RunEvent.seq)
        )
        rows = list(events)
    assert [event.seq for event in rows] == list(range(len(rows)))
    assert all(event.event != "old_event" for event in rows[:old_event_seq])
    assert rows[old_event_seq].event == "old_event"
    assert [event.event for event in rows].count("old_event") == 1
    assert len(rows) > old_event_seq + 1
    assert await checkpointer.aget_tuple(config) is not None


async def test_resume_reconciles_partial_fresh_retry_events_with_relational_history(
    session_factory,
) -> None:
    """A pending fresh-retry checkpoint continues from its own DB event slice."""
    from bidscope.graph.executor import create_run, execute

    old_event = {
        "node": "old_node",
        "event": "old_event",
        "status": "ok",
        "timestamp": "2026-07-18T09:00:00+00:00",
        "message": "old",
        "details": {"attempt": 0},
    }
    new_event_one = {
        "node": "new_node",
        "event": "new_event_one",
        "status": "ok",
        "timestamp": "2026-07-18T09:01:00+00:00",
        "message": "first fresh event",
        "details": {"attempt": 1},
    }
    new_event_two = {
        "node": "new_node",
        "event": "new_event_two",
        "status": "ok",
        "timestamp": "2026-07-18T09:02:00+00:00",
        "message": "resumed fresh event",
        "details": {"attempt": 1},
    }

    class PartialFreshRetryGraph:
        def __init__(self) -> None:
            self.phase = 0
            self.state: SimpleNamespace | None = SimpleNamespace(
                values={"node_events": [old_event], "event_seq_offset": 0},
                next=(),
            )
            self.checkpointer = self._Checkpointer(self)

        class _Checkpointer:
            def __init__(self, graph: PartialFreshRetryGraph) -> None:
                self.graph = graph

            async def adelete_thread(self, thread_id: str) -> None:
                assert thread_id == "same-thread"
                self.graph.state = None

        async def aget_state(self, config: Any) -> SimpleNamespace | None:
            assert config["configurable"]["thread_id"] == "same-thread"
            return self.state

        async def astream(self, input_data: Any, config: Any, stream_mode: str) -> Any:
            del input_data
            assert config["configurable"]["thread_id"] == "same-thread"
            assert stream_mode == "values"
            if self.phase == 0:
                self.phase += 1
                self.state = SimpleNamespace(
                    values={"node_events": [new_event_one], "event_seq_offset": 1},
                    next=("resume",),
                )
                yield self.state.values
                return

            assert self.phase == 1
            self.phase += 1
            self.state = SimpleNamespace(
                values={
                    "node_events": [new_event_one, new_event_two],
                    "event_seq_offset": 1,
                },
                next=(),
            )
            yield self.state.values

    run_id, created = await create_run("retry request", session_factory=session_factory)
    assert created is True
    execution_token = "fresh-retry-token"
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "running"
        run.execution_token = execution_token
        session.add(
            RunEvent(
                query_run_id=run_id,
                seq=0,
                timestamp=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
                node=old_event["node"],
                event=old_event["event"],
                status=old_event["status"],
                message=old_event["message"],
                details=old_event["details"],
            )
        )
        await session.commit()

    graph = PartialFreshRetryGraph()
    await execute(
        graph,
        run_id,
        {"user_request": "retry request"},
        session_factory=session_factory,
        checkpoint_thread_id="same-thread",
        force_fresh=True,
        execution_token=execution_token,
    )
    await execute(
        graph,
        run_id,
        {"user_request": "retry request"},
        session_factory=session_factory,
        checkpoint_thread_id="same-thread",
        execution_token=execution_token,
    )

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    sa.select(RunEvent)
                    .where(RunEvent.query_run_id == run_id)
                    .order_by(RunEvent.seq)
                )
            ).all()
        )

    assert [row.seq for row in rows] == [0, 1, 2]
    assert [row.event for row in rows] == [
        "old_event",
        "new_event_one",
        "new_event_two",
    ]


async def test_event_reconciliation_does_not_match_duplicate_fingerprint_from_old_attempt(
    session_factory,
) -> None:
    """A fresh attempt offset prevents an identical old event from being reused."""
    from bidscope.graph.executor import _reconcile_event_cursor, create_run

    event = {
        "node": "same_node",
        "event": "same_event",
        "status": "ok",
        "timestamp": "2026-07-18T09:00:00+00:00",
        "message": "same",
        "details": {"kind": "duplicate"},
    }
    run_id, created = await create_run("duplicate request", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        session.add_all(
            [
                RunEvent(
                    query_run_id=run_id,
                    seq=0,
                    timestamp=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
                    node=event["node"],
                    event=event["event"],
                    status=event["status"],
                    message=event["message"],
                    details=event["details"],
                ),
                RunEvent(
                    query_run_id=run_id,
                    seq=1,
                    timestamp=datetime(2026, 7, 18, 9, 1, tzinfo=UTC),
                    node=event["node"],
                    event=event["event"],
                    status=event["status"],
                    message=event["message"],
                    details=event["details"],
                ),
            ]
        )
        await session.commit()

    persisted, base = await _reconcile_event_cursor(
        run_id,
        [event],
        session_factory,
        event_seq_offset=2,
    )
    assert persisted == 0
    assert base == 2


async def test_nonterminal_checkpoint_accepts_exact_relational_event_prefix(
    session_factory,
) -> None:
    """A resumable checkpoint can repair events saved just before process loss."""
    from bidscope.graph.executor import _reconcile_event_cursor, create_run

    first = {
        "node": "node",
        "event": "first",
        "status": "ok",
        "message": None,
        "details": {},
        "timestamp": "2026-07-18T09:00:00+00:00",
    }
    second = {**first, "event": "second"}
    run_id, created = await create_run("checkpoint prefix", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        session.add(
            RunEvent(
                query_run_id=run_id,
                seq=4,
                timestamp=datetime(2026, 7, 18, 9, tzinfo=UTC),
                node=first["node"],
                event=first["event"],
                status=first["status"],
                message=first["message"],
                details=first["details"],
            )
        )
        await session.commit()

    assert await _reconcile_event_cursor(
        run_id,
        [first, second],
        session_factory,
        event_seq_offset=4,
    ) == (1, 4)


async def test_terminal_checkpoint_reconciles_events_before_short_circuit(
    session_factory,
) -> None:
    """A terminal checkpoint with missing relational events fails closed."""
    from bidscope.graph.executor import EventReconciliationError, create_run, execute

    run_id, created = await create_run("terminal reconciliation", session_factory=session_factory)
    assert created is True
    event = {
        "node": "node",
        "event": "done",
        "status": "ok",
        "message": None,
        "details": {},
        "timestamp": "2026-07-18T09:00:00+00:00",
    }

    class TerminalGraph:
        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(
                values={"status": "completed", "node_events": [event]},
                next=(),
            )

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("terminal checkpoint must not graph-stream")

    with pytest.raises(EventReconciliationError):
        await execute(
            TerminalGraph(),
            run_id,
            {"user_request": "terminal"},
            session_factory=session_factory,
        )


async def test_terminal_checkpoint_rejects_conflicting_relational_event(
    session_factory,
) -> None:
    """A terminal checkpoint conflict is not accepted as an idempotent replay."""
    from bidscope.graph.executor import EventReconciliationError, create_run, execute

    run_id, created = await create_run("terminal conflict", session_factory=session_factory)
    assert created is True
    local = {
        "node": "node",
        "event": "done",
        "status": "ok",
        "message": None,
        "details": {},
        "timestamp": "2026-07-18T09:00:00+00:00",
    }
    async with session_factory() as session:
        session.add(
            RunEvent(
                query_run_id=run_id,
                seq=0,
                timestamp=datetime(2026, 7, 18, 9, tzinfo=UTC),
                node="node",
                event="different",
                status="ok",
            )
        )
        await session.commit()

    class TerminalGraph:
        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(
                values={"status": "completed", "node_events": [local]},
                next=(),
            )

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("terminal checkpoint must not graph-stream")

    with pytest.raises(EventReconciliationError):
        await execute(
            TerminalGraph(),
            run_id,
            {"user_request": "terminal"},
            session_factory=session_factory,
        )


async def test_event_reconciliation_rejects_trailing_relational_event(
    session_factory,
) -> None:
    """A checkpoint prefix plus an unrepresented relational row fails closed."""
    from bidscope.graph.executor import (
        EventReconciliationError,
        _reconcile_event_cursor,
        create_run,
    )

    def event(name: str) -> dict[str, Any]:
        return {
            "node": "node",
            "event": name,
            "status": "ok",
            "timestamp": "2026-07-18T09:00:00+00:00",
            "message": name,
            "details": {},
        }

    local = [event("A"), event("B")]
    run_id, created = await create_run("trailing event", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        for seq, row in enumerate([*local, event("C")]):
            session.add(
                RunEvent(
                    query_run_id=run_id,
                    seq=seq,
                    timestamp=datetime(2026, 7, 18, 9, seq, tzinfo=UTC),
                    node=row["node"],
                    event=row["event"],
                    status=row["status"],
                    message=row["message"],
                    details=row["details"],
                )
            )
        await session.commit()

    with pytest.raises(EventReconciliationError):
        await _reconcile_event_cursor(
            run_id,
            local,
            session_factory,
            event_seq_offset=0,
        )


async def test_event_reconciliation_rejects_middle_sequence_mismatch(session_factory) -> None:
    """A mismatch at the expected sequence fails instead of selecting a suffix."""
    from bidscope.graph.executor import _reconcile_event_cursor, create_run

    def event(name: str) -> dict[str, Any]:
        return {
            "node": "node",
            "event": name,
            "status": "ok",
            "timestamp": "2026-07-18T09:00:00+00:00",
            "message": name,
            "details": {},
        }

    local = [event("A"), event("B"), event("C")]
    history = [event("A"), event("X"), event("C")]
    run_id, created = await create_run("mismatch request", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        for seq, row in enumerate(history, start=4):
            session.add(
                RunEvent(
                    query_run_id=run_id,
                    seq=seq,
                    timestamp=datetime(2026, 7, 18, 9, seq - 4, tzinfo=UTC),
                    node=row["node"],
                    event=row["event"],
                    status=row["status"],
                    message=row["message"],
                    details=row["details"],
                )
            )
        await session.commit()

    with pytest.raises(RuntimeError, match="sequence 5"):
        await _reconcile_event_cursor(
            run_id,
            local,
            session_factory,
            event_seq_offset=4,
        )


async def test_internal_checkpoint_write_rejects_superseded_execution_token(
    session_factory,
) -> None:
    """LangGraph saver writes inherit the active run's ownership fence."""
    from bidscope.graph.executor import (
        FencedCheckpointSaver,
        RunOwnershipLostError,
        create_run,
        execute,
    )
    from langgraph.checkpoint.base import BaseCheckpointSaver

    run_id, created = await create_run("checkpoint fence", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "running"
        run.execution_token = "old-token"
        await session.commit()

    class RecordingSaver(BaseCheckpointSaver[Any]):
        def __init__(self) -> None:
            super().__init__()
            self.put_calls = 0

        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.put_calls += 1
            return {"configurable": {"thread_id": run_id}}

    saver = RecordingSaver()

    class Graph:
        def __init__(self) -> None:
            self.checkpointer = FencedCheckpointSaver(saver)

        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={}, next=("node",))

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            async with session_factory() as session:
                run = await session.get(QueryRun, run_id)
                assert run is not None
                run.execution_token = "new-token"
                await session.commit()
            await self.checkpointer.aput({}, {}, {}, {})
            yield {}

    with pytest.raises(RunOwnershipLostError):
        await execute(
            Graph(),
            run_id,
            {"user_request": "checkpoint fence"},
            session_factory=session_factory,
            execution_token="old-token",
        )

    assert saver.put_calls == 0


async def test_force_fresh_requires_execution_token_before_deleting_checkpoint(
    session_factory,
) -> None:
    """Force-fresh checkpoint deletion is never available without ownership."""
    from bidscope.graph.executor import RunOwnershipLostError, create_run, execute

    run_id, created = await create_run("fresh token required", session_factory=session_factory)
    assert created is True

    class Checkpointer:
        def __init__(self) -> None:
            self.delete_calls = 0

        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            self.delete_calls += 1

    class Graph:
        def __init__(self) -> None:
            self.checkpointer = Checkpointer()

        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={}, next=("node",))

    graph = Graph()
    with pytest.raises(RunOwnershipLostError):
        await execute(
            graph,
            run_id,
            {"user_request": "fresh token required"},
            session_factory=session_factory,
            force_fresh=True,
        )

    assert graph.checkpointer.delete_calls == 0


async def test_force_fresh_rejects_old_token_before_deleting_new_checkpoint(
    session_factory,
) -> None:
    """A stale token cannot delete the checkpoint owned by a newer claim."""
    from bidscope.graph.executor import RunOwnershipLostError, create_run, execute

    run_id, created = await create_run("fresh ownership", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "running"
        run.execution_token = "new-token"
        await session.commit()

    class Checkpointer:
        def __init__(self) -> None:
            self.delete_calls = 0

        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            self.delete_calls += 1

    class Graph:
        def __init__(self) -> None:
            self.checkpointer = Checkpointer()

        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={"status": "retryable"}, next=("node",))

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("an old token must fail before graph execution")
            yield {}

    graph = Graph()
    with pytest.raises(RunOwnershipLostError):
        await execute(
            graph,
            run_id,
            {"user_request": "fresh ownership"},
            session_factory=session_factory,
            checkpoint_thread_id=run_id,
            force_fresh=True,
            execution_token="old-token",
        )

    assert graph.checkpointer.delete_calls == 0


async def test_force_fresh_rejects_cleared_old_token_before_deleting_checkpoint(
    session_factory,
) -> None:
    """A completed claim's old token cannot delete its terminal checkpoint."""
    from bidscope.graph.executor import RunOwnershipLostError, create_run, execute

    run_id, created = await create_run("cleared ownership", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "completed"
        run.execution_token = None
        await session.commit()

    class Checkpointer:
        def __init__(self) -> None:
            self.delete_calls = 0

        async def adelete_thread(self, thread_id: str) -> None:
            del thread_id
            self.delete_calls += 1

    class Graph:
        def __init__(self) -> None:
            self.checkpointer = Checkpointer()

        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={"status": "completed"}, next=())

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("an old token must fail before graph execution")
            yield {}

    graph = Graph()
    with pytest.raises(RunOwnershipLostError):
        await execute(
            graph,
            run_id,
            {"user_request": "cleared ownership"},
            session_factory=session_factory,
            checkpoint_thread_id=run_id,
            force_fresh=True,
            execution_token="old-token",
        )

    assert graph.checkpointer.delete_calls == 0


async def test_executor_rejects_missing_token_for_tokenized_run_before_graph_work(
    session_factory,
) -> None:
    """Direct executor calls cannot bypass a committed execution token."""
    from bidscope.graph.executor import RunOwnershipLostError, create_run, execute

    run_id, created = await create_run("token required", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "running"
        run.execution_token = "required-token"
        await session.commit()

    class Graph:
        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={}, next=("node",))

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("missing token must fail before graph execution")
            yield {}

    with pytest.raises(RunOwnershipLostError):
        await execute(
            Graph(),
            run_id,
            {"user_request": "token required"},
            session_factory=session_factory,
        )


async def test_terminal_checkpoint_requires_every_nonzero_offset_event(
    session_factory,
) -> None:
    """A terminal checkpoint cannot return when its offset event is missing."""
    from bidscope.graph.executor import EventReconciliationError, create_run, execute

    run_id, created = await create_run("terminal offset", session_factory=session_factory)
    assert created is True
    event = {
        "node": "node",
        "event": "done",
        "status": "ok",
        "message": None,
        "details": {},
        "timestamp": "2026-07-18T09:00:00+00:00",
    }

    class TerminalGraph:
        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(
                values={"status": "completed", "node_events": [event], "event_seq_offset": 4},
                next=(),
            )

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("terminal checkpoint must not graph-stream")
            yield {}

    with pytest.raises(EventReconciliationError):
        await execute(
            TerminalGraph(),
            run_id,
            {"user_request": "terminal offset"},
            session_factory=session_factory,
        )


async def test_retry_checkpoint_error_cancellation_repairs_after_delayed_status_write(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled checkpoint-error write still repairs a claimed run."""
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(
            QueryRun(
                id=run_id,
                run_key="delayed-retry-checkpoint-cancel",
                status="retryable",
                user_request="retry request",
            )
        )
        await session.commit()

    class FailingStateGraph:
        async def aget_state(self, config: Any) -> None:
            del config
            raise RuntimeError("checkpoint unavailable")

    service = RunService(
        session_factory=session_factory,
        graph=FailingStateGraph(),
        object_store=object(),
        settings=get_settings(),
    )
    original_update_status = service._update_status
    update_started = asyncio.Event()
    release_update = asyncio.Event()
    repair_started = asyncio.Event()
    release_repair = asyncio.Event()
    calls = 0

    async def delayed_update_status(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            update_started.set()
            await release_update.wait()
            return
        await original_update_status(*args, **kwargs)

    original_repair = service._repair_cancelled_claim

    async def delayed_repair(run_id: str, message: str) -> None:
        repair_started.set()
        await release_repair.wait()
        await original_repair(run_id, message)

    monkeypatch.setattr(service, "_update_status", delayed_update_status)
    monkeypatch.setattr(service, "_repair_cancelled_claim", delayed_repair)
    retry_task = asyncio.create_task(service.retry(run_id))
    await asyncio.wait_for(update_started.wait(), timeout=1)
    retry_task.cancel()
    release_update.set()
    await asyncio.wait_for(repair_started.wait(), timeout=1)
    retry_task.cancel()
    release_repair.set()
    with pytest.raises(asyncio.CancelledError):
        await retry_task

    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    assert run.status == "retryable"
    assert run.error == {
        "code": "graph_node_error",
        "message": "retry checkpoint lookup cancelled",
        "details": {},
    }
    assert calls == 2


async def test_terminal_retry_checkpoint_error_cancellation_repairs_claim(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during checkpoint-error persistence leaves retry eligible."""
    run_id = str(uuid.uuid4())
    async with session_factory() as session:
        session.add(QueryRun(
            id=run_id,
            run_key="terminal-retry-checkpoint-cancel",
            status="retryable",
            user_request="retry request",
        ))
        await session.commit()

    class FailingStateGraph:
        async def aget_state(self, config: Any) -> None:
            del config
            raise RuntimeError("checkpoint unavailable")

    service = RunService(
        session_factory=session_factory,
        graph=FailingStateGraph(),
        object_store=object(),
        settings=get_settings(),
    )
    original_update_status = service._update_status
    update_started = asyncio.Event()
    release_update = asyncio.Event()

    async def delayed_update_status(*args: Any, **kwargs: Any) -> None:
        update_started.set()
        await release_update.wait()
        await original_update_status(*args, **kwargs)

    monkeypatch.setattr(service, "_update_status", delayed_update_status)
    retry_task = asyncio.create_task(service.retry(run_id))
    await asyncio.wait_for(update_started.wait(), timeout=1)
    retry_task.cancel()
    release_update.set()
    with pytest.raises(asyncio.CancelledError):
        await retry_task

    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
    assert run is not None
    assert run.status == "retryable"
    assert run.error == {
        "code": "graph_node_error",
        "message": "checkpoint unavailable",
        "details": {},
    }
    second = await service.retry(run_id)
    assert second["status"] == "retryable"


async def test_executor_rejects_tokenless_call_on_retryable_cleared_token_run(
    session_factory,
) -> None:
    """A stale retryable row with cleared token rejects tokenless execution."""
    from bidscope.graph.executor import RunOwnershipLostError, create_run, execute

    run_id, created = await create_run("tokenless retryable", session_factory=session_factory)
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "retryable"
        run.execution_token = None
        await session.commit()

    class Graph:
        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={}, next=("node",))

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("tokenless call must fail before graph execution")
            yield {}

    with pytest.raises(RunOwnershipLostError):
        await execute(
            Graph(),
            run_id,
            {"user_request": "tokenless retryable"},
            session_factory=session_factory,
        )


async def test_append_events_rejects_tokenless_call_on_retryable_cleared_token_run(
    session_factory,
) -> None:
    """A stale retryable row with cleared token rejects tokenless event append."""
    from bidscope.graph.executor import RunOwnershipLostError, _append_events, create_run

    run_id, created = await create_run(
        "tokenless append retryable", session_factory=session_factory
    )
    assert created is True
    async with session_factory() as session:
        run = await session.get(QueryRun, run_id)
        assert run is not None
        run.status = "retryable"
        run.execution_token = None
        await session.commit()

    with pytest.raises(RunOwnershipLostError):
        await _append_events(
            run_id,
            [
                {
                    "timestamp": "2026-07-18T09:00:00+00:00",
                    "node": "stale",
                    "event": "must_not_persist",
                    "status": "ok",
                }
            ],
            0,
            session_factory,
        )
