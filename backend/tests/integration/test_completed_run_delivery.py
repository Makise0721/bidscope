"""Completed runs persist an online evidence-backed report before DOCX export."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from bidscope.api.dependencies import RunService, build_demo_graph
from bidscope.api.routes.reports import download_docx, get_report
from bidscope.clock import FixedClock
from bidscope.config import Settings, get_settings
from bidscope.delivery.objects import LocalObjectStore
from bidscope.delivery.reports import ReportPersistence
from bidscope.domain.notices import NoticeEvidence as DomainEvidence
from bidscope.domain.reports import Report, ReportCitation, ReportClaim, ReportItem
from bidscope.graph.executor import _to_plain_dsn
from bidscope.persistence.models import (
    NoticeEvidence,
    NoticeVersion,
    QueryRun,
    ReportClaimCitation,
)
from bidscope.persistence.models import (
    Report as ReportModel,
)
from bidscope.persistence.models import (
    ReportCitation as ReportCitationModel,
)
from bidscope.persistence.models import (
    ReportClaim as ReportClaimModel,
)
from bidscope.persistence.models import (
    ReportItem as ReportItemModel,
)
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _settings(tmp_path: Path) -> Settings:
    guarded_settings = get_settings()
    return Settings(
        app_mode="test",
        database_url=guarded_settings.database_url,
        checkpoint_database_url=guarded_settings.checkpoint_database_url,
        real_model_enabled=False,
        object_store_root=str(tmp_path / "graph-reports"),
    )


async def _count(session, model: type) -> int:
    return (await session.scalar(sa.select(sa.func.count()).select_from(model))) or 0


async def _seed_imported_report_inputs(
    session_factory, tmp_path: Path
) -> tuple[str, Report, dict[str, DomainEvidence]]:
    """Import a real demo notice and create a report using its immutable evidence."""
    async with session_factory() as session:
        await session.execute(sa.text("TRUNCATE snapshot_imports, snapshot_bundles CASCADE"))
        await session.commit()

    importer = SnapshotImporter(
        session_factory=session_factory,
        repository_factory=SnapshotRepository,
        object_store=LocalObjectStore(tmp_path / "snapshots"),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
    )
    await importer.import_bundle(PROJECT_ROOT / "data/demo/batch-1")

    async with session_factory() as session:
        version = await session.scalar(
            sa.select(NoticeVersion).order_by(NoticeVersion.id).limit(1)
        )
        assert version is not None
        evidence = await session.scalar(
            sa.select(NoticeEvidence)
            .where(NoticeEvidence.notice_version_id == version.id)
            .order_by(NoticeEvidence.id)
            .limit(1)
        )
        assert evidence is not None
        run = QueryRun(
            run_key="completed-delivery-direct",
            status="running",
            user_request="四川服务器招标",
        )
        session.add(run)
        await session.flush()
        run_id = str(run.id)
        await session.commit()

    domain_evidence = {
        evidence.span_hash: DomainEvidence(
            notice_version_id=str(version.id),
            text=evidence.text,
            start=evidence.start,
            end=evidence.end,
            span_hash=evidence.span_hash,
        )
    }
    report = Report(
        run_id=run_id,
        generated_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        query_conditions={"region": "四川"},
        items=[
            ReportItem(
                notice_id=str(version.id),
                title=version.title or "demo title",
                known_fields={"region": version.region or "四川"},
                unknown_fields=["deadline"],
                relevance_reason="服务器采购",
                risk_note="核验截止时间",
                citations=[
                    ReportCitation(evidence_id=evidence.span_hash, label="imported evidence")
                ],
                claims=[ReportClaim(text=evidence.text, citation_ids=[evidence.span_hash])],
            )
        ],
    )
    return run_id, report, domain_evidence


@pytest.mark.asyncio
async def test_persisted_online_report_keeps_evidence_links_and_is_idempotent(
    session_factory,
    tmp_path: Path,
) -> None:
    """A real imported evidence span backs one report, item, claim, and citation."""
    run_id, report, evidence_by_hash = await _seed_imported_report_inputs(session_factory, tmp_path)
    persistence = ReportPersistence(session_factory, LocalObjectStore(tmp_path / "reports"))

    first = await persistence.persist_online_report(report, evidence_by_hash)
    second = await persistence.persist_online_report(report, evidence_by_hash)
    export = await persistence.export_docx(first)

    assert first.id == second.id
    assert export.object_key
    assert persistence.store.exists(export.object_key)

    async with session_factory() as session:
        report_count = await session.scalar(
            sa.select(sa.func.count()).select_from(ReportModel).where(ReportModel.run_id == run_id)
        )
        assert report_count == 1
        assert await _count(session, ReportItemModel) == 1
        assert await _count(session, ReportClaimModel) == 1
        assert await _count(session, ReportCitationModel) == 1
        assert await _count(session, ReportClaimCitation) == 1


@pytest.mark.asyncio
async def test_completed_application_graph_persists_report_and_serves_docx(
    session_factory,
    tmp_path: Path,
) -> None:
    """Imported demo data flows through the real graph, API DTO, and DOCX route."""
    await _seed_imported_report_inputs(session_factory, tmp_path)
    object_store = LocalObjectStore(tmp_path / "graph-reports")
    settings = _settings(tmp_path)
    clock = FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC))
    dsn = _to_plain_dsn(settings.checkpoint_database_dsn())
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        graph = build_demo_graph(
            session_factory,
            settings,
            checkpointer=checkpointer,
            clock=clock,
            object_store=object_store,
        )
        service = RunService(session_factory, graph, object_store, settings, clock=clock)
        run_id, created = await service.create_run("四川省服务器招标")
        assert created is True

        result = await service.execute_run(run_id, {"user_request": "四川省服务器招标"})
        assert result["status"] == "completed", result

        body = await get_report(run_id, service)
        assert body["items"]
        item = body["items"][0]
        assert item["claims"]
        assert item["citations"]
        assert item["citations"][0]["evidence_id"]
        assert item["citations"][0]["span_hash"]
        assert item["provenance"]["source_version_id"]
        response = await download_docx(run_id, service)
        assert response.status_code == 200
        assert response.body

        async with session_factory() as session:
            assert await _count(session, ReportModel) == 1
            assert await _count(session, ReportItemModel) >= 1
            assert await _count(session, ReportClaimModel) >= 1
            assert await _count(session, ReportClaimCitation) >= 1


@pytest.mark.asyncio
async def test_mismatched_evidence_never_commits_a_report(
    session_factory,
    tmp_path: Path,
) -> None:
    """An evidence binding from another notice version aborts the full transaction."""
    _, report, evidence_by_hash = await _seed_imported_report_inputs(session_factory, tmp_path)
    evidence = next(iter(evidence_by_hash.values()))
    foreign = evidence.model_copy(
        update={"notice_version_id": "00000000-0000-0000-0000-000000000999"}
    )
    persistence = ReportPersistence(session_factory, LocalObjectStore(tmp_path / "reports"))

    from bidscope.delivery.docx import DeliveryError

    with pytest.raises(DeliveryError):
        await persistence.persist_online_report(report, {foreign.span_hash: foreign})

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(ReportModel)) == 0
