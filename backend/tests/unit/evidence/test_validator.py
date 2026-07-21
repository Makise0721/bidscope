"""Unit tests for the pure evidence-validator layer.

These target :func:`~bidscope.evidence.validator.validate_claim` and
:func:`~bidscope.evidence.validator.validate_report` directly, without the
graph. Every reported claim must cite evidence that exists, belongs to the
same notice version, sits at valid offsets and matches its stored span hash.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bidscope.domain.notices import NoticeEvidence
from bidscope.domain.reports import Report, ReportCitation, ReportClaim, ReportItem
from bidscope.evidence.extractor import extract_evidence
from bidscope.evidence.validator import (
    validate_claim,
    validate_report,
)


def _generated_at() -> datetime:
    return datetime(2026, 7, 18, tzinfo=UTC)


def _evidence(
    notice_version_id: str = "version-1",
    text: str = "预算金额：680万元。",
) -> NoticeEvidence:
    """Build a canonical :class:`NoticeEvidence` with a consistent span hash."""
    spans = extract_evidence(notice_version_id, text, (text,))
    return spans[0]


def _evidence_by_id(*evidences: NoticeEvidence) -> dict[str, NoticeEvidence]:
    return {f"ev-{index}": evidence for index, evidence in enumerate(evidences)}


def test_claim_must_reference_same_notice_version() -> None:
    """A citation pointing at a different notice version is rejected."""
    evidence = _evidence(notice_version_id="version-1")
    result = validate_claim(
        claim=ReportClaim(text="预算680万", citation_ids=["ev-0"]),
        item_version_id="version-new",
        evidence_by_id=_evidence_by_id(evidence),
    )
    assert result.valid is False
    assert result.errors == ("citation_version_mismatch",)


def test_claim_with_missing_evidence_is_rejected() -> None:
    """A citation id with no matching evidence span is rejected."""
    result = validate_claim(
        claim=ReportClaim(text="无来源", citation_ids=["ev-missing"]),
        item_version_id="version-1",
        evidence_by_id={},
    )
    assert result.valid is False
    assert result.errors == ("missing_evidence",)


def test_claim_without_citation_is_rejected() -> None:
    """A claim that cites nothing is unsupported.

    ``ReportClaim``'s own Pydantic validator already rejects empty citations at
    construction, so this path is defence in depth; we bypass that constructor
    check with ``model_construct`` to exercise the validator directly.
    """
    claim = ReportClaim.model_construct(text="无引用", citation_ids=[])
    result = validate_claim(
        claim=claim,
        item_version_id="version-1",
        evidence_by_id={},
    )
    assert result.valid is False
    assert result.errors == ("claim_without_citation",)


def test_tampered_span_hash_is_detected() -> None:
    """A stored span hash that no longer matches the text is rejected."""
    evidence = _evidence(text="预算金额：680万元。")
    tampered = NoticeEvidence(
        notice_version_id=evidence.notice_version_id,
        text=evidence.text,
        start=evidence.start,
        end=evidence.end,
        span_hash="deadbeef",
    )
    result = validate_claim(
        claim=ReportClaim(text="预算680万", citation_ids=["ev-0"]),
        item_version_id="version-1",
        evidence_by_id={"ev-0": tampered},
    )
    assert result.valid is False
    assert "span_hash_mismatch" in result.errors


def test_valid_claim_passes_all_checks() -> None:
    """A claim citing intact, same-version evidence is accepted."""
    evidence = _evidence(notice_version_id="version-1")
    result = validate_claim(
        claim=ReportClaim(text="预算680万", citation_ids=["ev-0"]),
        item_version_id="version-1",
        evidence_by_id=_evidence_by_id(evidence),
    )
    assert result.valid is True
    assert result.errors == ()


def test_validate_report_aggregates_errors() -> None:
    """A report is invalid when any of its claims is unsupported."""
    evidence = _evidence(notice_version_id="version-1")
    valid_claim = ReportClaim(text="预算680万", citation_ids=["ev-0"])
    bad_claim = ReportClaim(text="无来源", citation_ids=["ev-missing"])
    report = Report(
        run_id="r",
        generated_at=_generated_at(),
        query_conditions={},
        items=[
            ReportItem(
                notice_id="version-1",
                title="t",
                claims=[valid_claim, bad_claim],
                citations=[ReportCitation(evidence_id="ev-0")],
            )
        ],
    )
    result = validate_report(report, _evidence_by_id(evidence))
    assert result.valid is False
    assert "missing_evidence" in result.errors


def test_validate_report_accepts_fully_supported_report() -> None:
    """A report whose every claim cites valid evidence passes."""
    evidence = _evidence(notice_version_id="version-1")
    claim = ReportClaim(text="预算680万", citation_ids=["ev-0"])
    report = Report(
        run_id="r",
        generated_at=_generated_at(),
        query_conditions={},
        items=[ReportItem(
            notice_id="version-1", title="t", claims=[claim],
            citations=[ReportCitation(evidence_id="ev-0")],
        )],
    )
    result = validate_report(report, _evidence_by_id(evidence))
    assert result.valid is True
    assert result.errors == ()
