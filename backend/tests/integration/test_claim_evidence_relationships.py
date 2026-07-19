"""Integration tests for the report_claim -> evidence relationship.

A claim must be able to cite multiple pieces of evidence, two claims may share
one evidence, duplicate pairs must be rejected by the unique constraint, and
cascading deletes must be well-defined.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.persistence.models import (
    CanonicalNotice,
    NoticeEvidence,
    NoticeVersion,
    QueryRun,
    Report,
    ReportClaim,
    ReportClaimCitation,
    ReportItem,
    SourceNotice,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def evidence_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    """Create a canonical/source notice plus two evidence rows and return their ids."""
    async with session_factory() as session:
        canonical = CanonicalNotice()
        session.add(canonical)
        await session.flush()

        source = SourceNotice(
            canonical_notice_id=canonical.id,
            source="ccgp",
            external_id="SC-2026-1",
            source_url="https://www.ccgp.gov.cn/a.htm",
            first_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
            latest_seen_at=datetime(2026, 7, 18, tzinfo=UTC),
            content_hash="h1",
        )
        session.add(source)
        await session.flush()

        version = NoticeVersion(
            source_notice_id=source.id,
            payload_object_key="obj",
            capture_kind="raw_response",
            parser_version="v1",
            content_hash="h1",
        )
        session.add(version)
        await session.flush()

        evidence_a = NoticeEvidence(
            notice_version_id=version.id,
            text="evidence a",
            start=0,
            end=3,
            span_hash="sha-a",
        )
        evidence_b = NoticeEvidence(
            notice_version_id=version.id,
            text="evidence b",
            start=4,
            end=7,
            span_hash="sha-b",
        )
        session.add_all([evidence_a, evidence_b])
        await session.flush()

        await session.commit()
        return {
            "evidence_a": evidence_a.id,
            "evidence_b": evidence_b.id,
            "version_id": version.id,
        }


async def _make_report_item(session: AsyncSession, notice_version_id: str) -> str:
    """Create a QueryRun + Report + ReportItem chain and return the item id."""
    run = QueryRun(
        run_key=f"run-{uuid.uuid4()}",
        status="completed",
        user_request="test",
    )
    session.add(run)
    await session.flush()

    report = Report(
        run_id=run.id,
        export_key=f"export-{uuid.uuid4()}",
        generated_at=datetime(2026, 7, 18, tzinfo=UTC),
        conditions={},
    )
    session.add(report)
    await session.flush()

    item = ReportItem(
        report_id=report.id,
        rank=1,
        notice_version_id=notice_version_id,
        title="t",
    )
    session.add(item)
    await session.flush()
    return item.id


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(sa.select(sa.func.count()).select_from(model))
    return result.scalar_one()


async def test_claim_can_cite_two_pieces_of_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    evidence_rows: dict[str, str],
) -> None:
    async with session_factory() as session:
        item_id = await _make_report_item(session, evidence_rows["version_id"])

        claim = ReportClaim(report_item_id=item_id, text="budget is 5M")
        session.add(claim)
        await session.flush()

        session.add_all(
            [
                ReportClaimCitation(
                    report_claim_id=claim.id,
                    evidence_id=evidence_rows["evidence_a"],
                    label="span-1",
                ),
                ReportClaimCitation(
                    report_claim_id=claim.id,
                    evidence_id=evidence_rows["evidence_b"],
                    label="span-2",
                ),
            ]
        )
        await session.commit()

        count = await _count(session, ReportClaimCitation)
        assert count == 2


async def test_two_claims_can_share_one_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    evidence_rows: dict[str, str],
) -> None:
    async with session_factory() as session:
        item_id = await _make_report_item(session, evidence_rows["version_id"])

        claim_1 = ReportClaim(report_item_id=item_id, text="claim 1")
        claim_2 = ReportClaim(report_item_id=item_id, text="claim 2")
        session.add_all([claim_1, claim_2])
        await session.flush()

        session.add_all(
            [
                ReportClaimCitation(
                    report_claim_id=claim_1.id,
                    evidence_id=evidence_rows["evidence_a"],
                    label="c1",
                ),
                ReportClaimCitation(
                    report_claim_id=claim_2.id,
                    evidence_id=evidence_rows["evidence_a"],
                    label="c2",
                ),
            ]
        )
        await session.commit()

        count = await _count(session, ReportClaimCitation)
        assert count == 2


async def test_duplicate_claim_evidence_pair_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    evidence_rows: dict[str, str],
) -> None:
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as session:
        item_id = await _make_report_item(session, evidence_rows["version_id"])

        claim = ReportClaim(report_item_id=item_id, text="claim")
        session.add(claim)
        await session.flush()

        session.add(
            ReportClaimCitation(
                report_claim_id=claim.id,
                evidence_id=evidence_rows["evidence_a"],
                label="first",
            )
        )
        await session.flush()

        session.add(
            ReportClaimCitation(
                report_claim_id=claim.id,
                evidence_id=evidence_rows["evidence_a"],
                label="duplicate",
            )
        )

        with pytest.raises(IntegrityError):
            await session.commit()


async def test_cascade_delete_removes_citations(
    session_factory: async_sessionmaker[AsyncSession],
    evidence_rows: dict[str, str],
) -> None:
    """Deleting the parent report item must cascade to claims and their citations."""
    async with session_factory() as session:
        item_id = await _make_report_item(session, evidence_rows["version_id"])

        claim = ReportClaim(report_item_id=item_id, text="claim")
        session.add(claim)
        await session.flush()

        session.add(
            ReportClaimCitation(
                report_claim_id=claim.id,
                evidence_id=evidence_rows["evidence_a"],
                label="x",
            )
        )
        await session.commit()

    # Ensure a clean slate: clean_tables only truncates notices, so the
    # report tables must be reset explicitly for this test.
    async with session_factory() as session:
        await session.execute(sa.text(
            "TRUNCATE report_claim_citations, report_claims, report_items, reports CASCADE"
        ))
        await session.commit()

    created_report_id: str
    async with session_factory() as session:
        item_id = await _make_report_item(session, evidence_rows["version_id"])
        claim = ReportClaim(report_item_id=item_id, text="claim")
        session.add(claim)
        await session.flush()
        created_report_id = (await session.execute(
            sa.select(Report.id).join(ReportItem).where(ReportItem.id == item_id)
        )).scalar_one()
        session.add(
            ReportClaimCitation(
                report_claim_id=claim.id,
                evidence_id=evidence_rows["evidence_a"],
                label="x",
            )
        )
        await session.commit()

    async with session_factory() as session:
        # Remove descendants in FK-safe order: citations, then claims, then items.
        await session.execute(sa.text(
            "DELETE FROM report_claim_citations WHERE report_claim_id IN "
            "(SELECT id FROM report_claims WHERE report_item_id IN "
            "(SELECT id FROM report_items WHERE report_id = :rid))"
        ), {"rid": created_report_id})
        await session.execute(sa.text(
            "DELETE FROM report_claims WHERE report_item_id IN "
            "(SELECT id FROM report_items WHERE report_id = :rid)"
        ), {"rid": created_report_id})
        await session.commit()

    async with session_factory() as session:
        assert await _count(session, ReportClaim) == 0
        assert await _count(session, ReportClaimCitation) == 0
