"""Pure verification that a report's claims are backed by immutable evidence.

The validator never touches the network, the database or the current time: it
operates entirely on the :class:`~bidscope.domain.reports.Report` produced by
the synthesis step and the :class:`~bidscope.domain.notices.NoticeEvidence`
spans built by :mod:`bidscope.evidence.extractor`. A claim is only accepted
when every citation resolves to an existing span belonging to the same notice
version, at character offsets consistent with the source text and matching its
stored span hash. Anything less is an unsupported claim and is rejected so the
graph never delivers a report containing it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from bidscope.domain.notices import NoticeEvidence
from bidscope.domain.reports import Report, ReportClaim
from bidscope.llm.types import ReportDraft

#: A report-like object the validator accepts: a trusted :class:`Report` or the
#: :class:`ReportDraft` produced by synthesis before it is promoted.
ReportLike = Report | ReportDraft
EvidenceById = Mapping[str, NoticeEvidence | list[NoticeEvidence] | tuple[NoticeEvidence, ...]]


@dataclass(frozen=True)
class ClaimValidationResult:
    """The outcome of validating a single :class:`~bidscope.domain.reports.ReportClaim`."""

    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ReportValidationResult:
    """The outcome of validating a whole :class:`~bidscope.domain.reports.Report`."""

    valid: bool
    errors: tuple[str, ...]


def _hash(text: str) -> str:
    """SHA-256 digest used as the span-stability check throughout the layer."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_claim(
    claim: ReportClaim,
    item_version_id: str,
    evidence_by_id: EvidenceById,
) -> ClaimValidationResult:
    """Check that every citation in ``claim`` resolves to valid evidence.

    The ``item_version_id`` is the notice version the reported item is bound to
    (for P0 the item's own ``notice_id``); each cited evidence span must belong
    to that same version.
    """
    errors: list[str] = []

    if not claim.citation_ids:
        return ClaimValidationResult(valid=False, errors=("claim_without_citation",))

    for citation_id in claim.citation_ids:
        binding = evidence_by_id.get(citation_id)
        if binding is None:
            errors.append("missing_evidence")
            continue
        candidates = binding if isinstance(binding, (list, tuple)) else (binding,)
        evidence = next(
            (
                candidate
                for candidate in candidates
                if candidate.notice_version_id == item_version_id
            ),
            None,
        )
        if evidence is None:
            errors.append("citation_version_mismatch")
            continue
        if evidence.start < 0 or evidence.end < evidence.start:
            errors.append("invalid_offset")
        text_length = len(evidence.text)
        if evidence.end - evidence.start != text_length:
            errors.append("invalid_offset")
        if evidence.span_hash != _hash(evidence.text):
            errors.append("span_hash_mismatch")

    return ClaimValidationResult(valid=not errors, errors=tuple(errors))


def validate_report(
    report: ReportLike,
    evidence_by_id: EvidenceById,
) -> ReportValidationResult:
    """Validate every claim in a :class:`~bidscope.domain.reports.Report`.

    Each item is bound to its own ``notice_id`` (used as the version id for the
    version-consistency check). The report is only valid when every claim in
    every item cites intact evidence.
    """
    all_errors: list[str] = []

    for item in report.items:
        for claim in item.claims:
            result = validate_claim(
                claim, item_version_id=item.notice_id, evidence_by_id=evidence_by_id
            )
            if not result.valid:
                all_errors.extend(result.errors)

    # Deduplicate while preserving first-seen order so error reporting is stable.
    seen: set[str] = set()
    unique: list[str] = []
    for error in all_errors:
        if error not in seen:
            seen.add(error)
            unique.append(error)

    return ReportValidationResult(valid=not unique, errors=tuple(unique))


def hash_span(text: str) -> str:
    """Public span-hash helper used by tests and the extractor."""
    return _hash(text)


__all__ = [
    "ClaimValidationResult",
    "ReportValidationResult",
    "hash_span",
    "validate_claim",
    "validate_report",
]

# Keep an explicit reference so ``hashlib`` imports are honoured even if the
# private ``_hash`` helper is ever inlined.
_ = hashlib
