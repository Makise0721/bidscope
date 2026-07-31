"""Integration coverage for Semantic Citation Contract verdict persistence.

Verifies that the full judgment record (status, rationale, evidence ids used,
conflict evidence ids, verifier version) is persisted transactionally with the
report, survives a replay round-trip, and is exposed by the report API as the
main intelligence list (SUPPORTED only) plus a review partition
(UNSUPPORTED/UNCERTAIN with their verification records).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from bidscope.api.dependencies import RunService
from bidscope.api.routes.reports import get_report
from bidscope.config import Settings
from bidscope.delivery.objects import LocalObjectStore
from bidscope.delivery.reports import ReportPersistence
from bidscope.domain.enums import ClaimSupportStatus
from bidscope.domain.notices import NoticeEvidence as DomainEvidence
from bidscope.domain.reports import (
    Report,
    ReportCitation,
    ReportClaim,
    ReportItem,
)
from bidscope.evidence.semantic_verifier import ClaimSupportVerification, ClaimVerification
from bidscope.persistence.models import (
    CanonicalNotice,
    NoticeEvidence,
    NoticeVersion,
    QueryRun,
    ReportClaimVerification,
    SourceNotice,
)


async def _seed_report_inputs(session_factory) -> tuple[Report, dict[str, DomainEvidence]]:
    """One notice version with one evidence span and one claim (see delivery tests)."""
    run_id = str(uuid.uuid4())
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        canonical = CanonicalNotice(title="四川智算中心服务器采购招标公告")
        session.add(canonical)
        await session.flush()
        source = SourceNotice(
            canonical_notice_id=canonical.id,
            source="synthetic_demo",
            external_id="claim-verification-demo",
            source_url="https://example.invalid/claim-verification-demo",
            first_seen_at=now,
            latest_seen_at=now,
            content_hash="notice-hash",
            title="四川智算中心服务器采购招标公告",
        )
        session.add(source)
        await session.flush()
        version = NoticeVersion(
            source_notice_id=source.id,
            payload_object_key="snapshots/demo/claim-verification-demo",
            capture_kind="synthetic_demo",
            parser_version="demo-v1",
            content_hash="version-hash",
            title=source.title,
            region="四川",
        )
        session.add(version)
        await session.flush()
        evidence = NoticeEvidence(
            notice_version_id=version.id,
            text="预算800万元。",
            start=0,
            end=8,
            span_hash="evidence-001",
        )
        session.add(evidence)
        session.add(QueryRun(
            id=run_id,
            run_key=run_id,
            status="running",
            user_request="test",
        ))
        await session.commit()

    domain_evidence = {
        evidence.span_hash: DomainEvidence(
            notice_version_id=str(version.id),
            text=evidence.text,
            start=evidence.start,
            end=evidence.end,
            span_hash=evidence.span_hash,
        ),
    }
    report = Report(
        run_id=run_id,
        generated_at=now,
        query_conditions={"region": "四川"},
        items=[
            ReportItem(
                notice_id=str(version.id),
                title=version.title or "",
                known_fields={"source_url": source.source_url},
                unknown_fields=["deadline"],
                citations=[ReportCitation(evidence_id=evidence.span_hash, label="预算金额证据")],
                claims=[ReportClaim(text=evidence.text, citation_ids=[evidence.span_hash])],
            )
        ],
    )
    return report, domain_evidence


def _verifications(notice_id: str, evidence_hash: str) -> list[ClaimVerification]:
    """Two verdicts for the item's claims: SUPPORTED and UNSUPPORTED."""
    return [
        ClaimVerification(
            notice_id=notice_id,
            claim_index=0,
            verification=ClaimSupportVerification(
                status=ClaimSupportStatus.SUPPORTED,
                rationale="证据明确记载了与 Claim 一致的金额。",
                evidence_ids_used=(evidence_hash,),
                conflict_evidence_ids=(),
                verifier_version="fake-deterministic-v1",
            ),
        ),
        ClaimVerification(
            notice_id=notice_id,
            claim_index=1,
            verification=ClaimSupportVerification(
                status=ClaimSupportStatus.UNSUPPORTED,
                rationale="证据中记载的金额与 Claim 不一致。",
                evidence_ids_used=(evidence_hash,),
                conflict_evidence_ids=(evidence_hash,),
                verifier_version="fake-deterministic-v1",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_persist_claim_verifications_and_replay_status(
    session_factory, tmp_path
) -> None:
    report, evidence_by_hash = await _seed_report_inputs(session_factory)
    item = report.items[0]
    item = item.model_copy(update={
        "claims": [
            item.claims[0],
            ReportClaim(
                text="预算为 999 万元。",
                citation_ids=["evidence-001"],
            ),
        ],
    })
    report = report.model_copy(update={"items": [item]})
    verifications = _verifications(item.notice_id, "evidence-001")
    persistence = ReportPersistence(session_factory, LocalObjectStore(tmp_path / "objects"))

    await persistence.persist_online_report(
        report, evidence_by_hash, claim_verifications=verifications
    )

    # The first-call returned report echoes the input; the durable status lives
    # in the verification rows and in the replay projection below.
    async with session_factory() as session:
        rows = list((await session.scalars(
            sa.select(ReportClaimVerification)
        )).all())
        assert len(rows) == 2
        by_status = {row.status: row for row in rows}
        supported = by_status["supported"]
        assert supported.rationale == "证据明确记载了与 Claim 一致的金额。"
        assert supported.evidence_ids_used == ["evidence-001"]
        assert supported.conflict_evidence_ids == []
        assert supported.verifier_version == "fake-deterministic-v1"
        unsupported = by_status["unsupported"]
        assert unsupported.conflict_evidence_ids == ["evidence-001"]

    # Replay must reproduce the verdict status on the projected report.
    replayed = await persistence.persist_online_report(
        report, evidence_by_hash, claim_verifications=verifications
    )
    replayed_claims = replayed.report.items[0].claims
    assert [c.support_status for c in replayed_claims] == [
        ClaimSupportStatus.SUPPORTED,
        ClaimSupportStatus.UNSUPPORTED,
    ]


@pytest.mark.asyncio
async def test_api_splits_supported_and_review_claims(session_factory, tmp_path) -> None:
    report, evidence_by_hash = await _seed_report_inputs(session_factory)
    item = report.items[0]
    item = item.model_copy(update={
        "claims": [
            item.claims[0],
            ReportClaim(text="预算为 999 万元。", citation_ids=["evidence-001"]),
        ],
    })
    report = report.model_copy(update={"items": [item]})
    verifications = _verifications(item.notice_id, "evidence-001")
    persistence = ReportPersistence(session_factory, LocalObjectStore(tmp_path / "objects"))
    await persistence.persist_online_report(
        report, evidence_by_hash, claim_verifications=verifications
    )

    service = RunService(
        session_factory,
        graph=object(),
        object_store=persistence.store,
        settings=Settings(app_mode="test"),
    )
    body = await get_report(report.run_id, service)
    api_item = body["items"][0]

    # Main intelligence list: only SUPPORTED claims, no review items inside.
    assert [claim["text"] for claim in api_item["claims"]] == ["预算800万元。"]
    assert api_item["claims"][0]["support_status"] == "supported"

    # Review queue: UNSUPPORTED claim carries the full verification record.
    review = api_item["review_claims"]
    assert [claim["text"] for claim in review] == ["预算为 999 万元。"]
    assert review[0]["support_status"] == "unsupported"
    assert review[0]["verification"]["status"] == "unsupported"
    assert review[0]["verification"]["conflict_evidence_ids"] == ["evidence-001"]
    assert review[0]["verification"]["verifier_version"] == "fake-deterministic-v1"
    assert "review_claims" not in api_item.get("claims", {})


@pytest.mark.asyncio
async def test_api_lists_unverified_claims_in_main_list(session_factory, tmp_path) -> None:
    """Claims never semantically verified stay visible (no status, no review split)."""
    report, evidence_by_hash = await _seed_report_inputs(session_factory)
    persistence = ReportPersistence(session_factory, LocalObjectStore(tmp_path / "objects"))
    await persistence.persist_online_report(report, evidence_by_hash)

    service = RunService(
        session_factory,
        graph=object(),
        object_store=persistence.store,
        settings=Settings(app_mode="test"),
    )
    body = await get_report(report.run_id, service)
    api_item = body["items"][0]

    assert [claim["text"] for claim in api_item["claims"]] == ["预算800万元。"]
    assert "support_status" not in api_item["claims"][0]
    assert "review_claims" not in api_item
