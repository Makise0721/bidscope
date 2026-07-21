"""Deterministic deduplication and material-change detection.

Two pure functions over a value object (:class:`NoticeView`) that never touches
the database or the current time:

* :func:`classify_duplicate` — decide whether two notices are ``exact``
  duplicates (strong deterministic evidence), ``distinct`` (clear conflict), or
  ``ambiguous`` (all other pairs, left to the model port later).
* :func:`detect_material_changes` — enumerate the business fields that changed
  between two versions of a notice, ignoring formatting-only differences.

The outputs are bounded and serializable so downstream stages (the report view
and the evaluation suite) can consume them without surprises.
"""

from __future__ import annotations

import unicodedata
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from bidscope.domain.types import AwareDatetime


@dataclass(frozen=True)
class NoticeView:
    """The slice of a notice the dedup/change detectors operate on.

    ``deadline`` must be timezone-aware; a naive datetime is rejected at
    construction so the pure functions below never have to branch on tz-awareness.
    """

    source: str
    external_id: str
    canonical_url: str
    project_number: str | None
    content_hash: str
    title: str | None = None
    purchaser: str | None = None
    region: str | None = None
    budget_minor_units: int | None = None
    budget_currency: str | None = None
    deadline: AwareDatetime | None = None
    procurement_scope: str | None = None
    cancellation: bool = False
    claim_supporting_texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Frozen-field enforcement of the domain's tz-aware contract; mirrors the
        # Pydantic ``AwareDatetime`` validator but at the dataclass boundary.
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("NoticeView.deadline must be timezone-aware")


class DuplicateDecision:
    """Bounded dedup decisions."""

    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    DISTINCT = "distinct"


@dataclass(frozen=True)
class DuplicateClassification:
    decision: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


class MaterialChangeType:
    """Bounded set of material fields."""

    DEADLINE = "deadline"
    BUDGET = "budget"
    REGION = "region"
    PURCHASER = "purchaser"
    SCOPE = "scope"
    CANCELLATION = "cancellation"
    CLAIM_EVIDENCE = "claim_evidence"


@dataclass(frozen=True)
class MaterialChange:
    field: str
    before: str
    after: str


def _normalize_text(value: str | None) -> str:
    """Normalize text for material-change comparison.

    Collapses consecutive whitespace, strips leading/trailing whitespace, and
    applies Unicode NFKC normalization so common equivalent forms compare
    equal. Returns an empty string for ``None`` inputs.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    return normalized


def _canonicalize_url(url: str) -> str:
    """Return a canonical form of a source URL for equality comparison.

    Drops the query string and fragment (e.g. tracking parameters and
    sections) and lowercases the scheme/host so equivalent notices match.
    """
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def classify_duplicate(
    candidate: NoticeView,
    existing: NoticeView,
    *,
    equivalent_ids: Iterable[tuple[str, str]] = (),
) -> DuplicateClassification:
    """Decide whether two notices are exact duplicates, distinct, or ambiguous.

    ``exact`` requires strong deterministic evidence: a shared content hash,
    a shared non-empty project number, a shared source + canonical URL, or an
    explicit source-ID equivalence mapping. ``distinct`` requires clear
    conflict evidence (differing project numbers plus a purchaser/budget/region
    conflict). Every other pair is ``ambiguous`` and deferred to the model
    port in a later task.
    """
    reasons: list[str] = []

    if candidate.content_hash == existing.content_hash:
        reasons.append(f"identical content hash: {candidate.content_hash}")

    candidate_number = candidate.project_number.strip() if candidate.project_number else ""
    existing_number = existing.project_number.strip() if existing.project_number else ""
    if candidate_number and existing_number and candidate_number == existing_number:
        reasons.append(f"identical project number: {candidate_number}")

    if (
        candidate.source == existing.source
        and _canonicalize_url(candidate.canonical_url)
        == _canonicalize_url(existing.canonical_url)
    ):
        reasons.append(
            f"identical source and canonical URL: {candidate.source} "
            f"{_canonicalize_url(candidate.canonical_url)}"
        )

    pair = {candidate.source, existing.source}
    for left, right in equivalent_ids:
        if {left, right} == pair:
            reasons.append(f"explicit source-ID mapping: {left} ↔ {right}")
            break

    if reasons:
        return DuplicateClassification(decision=DuplicateDecision.EXACT, reasons=tuple(reasons))

    distinct_reasons = _distinct_reasons(candidate, existing)
    if distinct_reasons:
        return DuplicateClassification(
            decision=DuplicateDecision.DISTINCT, reasons=tuple(distinct_reasons)
        )

    return DuplicateClassification(
        decision=DuplicateDecision.AMBIGUOUS,
        reasons=("no strong exact or distinct evidence",),
    )


def _distinct_reasons(candidate: NoticeView, existing: NoticeView) -> list[str]:
    """Detect clear conflict evidence between two notices."""
    reasons: list[str] = []

    both_have_project_numbers = bool(candidate.project_number and existing.project_number)
    if both_have_project_numbers and candidate.project_number != existing.project_number:
        conflicts: list[str] = []
        if _normalize_text(candidate.purchaser) != _normalize_text(existing.purchaser):
            conflicts.append("purchaser")
        if candidate.budget_minor_units != existing.budget_minor_units:
            conflicts.append("budget")
        if _normalize_text(candidate.region) != _normalize_text(existing.region):
            conflicts.append("region")
        if conflicts:
            reasons.append(
                f"project numbers differ ({candidate.project_number} vs "
                f"{existing.project_number}) and fields conflict: "
                f"{', '.join(conflicts)}"
            )
            return reasons

    return reasons


def detect_material_changes(old: NoticeView, new: NoticeView) -> list[MaterialChange]:
    """Return the material fields that changed between two notice versions.

    Formatting-only differences are ignored via :func:`_normalize_text`.
    Changes are returned in a stable, evaluation-friendly field order. The
    function is pure: it never reads the current time.
    """
    changes: list[MaterialChange] = []

    if old.deadline != new.deadline:
        changes.append(
            MaterialChange(
                field="deadline",
                before=_format_datetime(old.deadline),
                after=_format_datetime(new.deadline),
            )
        )

    if old.budget_minor_units != new.budget_minor_units:
        changes.append(
            MaterialChange(
                field="budget",
                before=_format_money(old.budget_minor_units, old.budget_currency),
                after=_format_money(new.budget_minor_units, new.budget_currency),
            )
        )

    if _normalize_text(old.region) != _normalize_text(new.region):
        changes.append(
            MaterialChange(field="region", before=old.region or "", after=new.region or "")
        )

    if _normalize_text(old.purchaser) != _normalize_text(new.purchaser):
        changes.append(
            MaterialChange(
                field="purchaser", before=old.purchaser or "", after=new.purchaser or ""
            )
        )

    if _normalize_text(old.procurement_scope) != _normalize_text(new.procurement_scope):
        changes.append(
            MaterialChange(
                field="scope",
                before=old.procurement_scope or "",
                after=new.procurement_scope or "",
            )
        )

    if old.cancellation != new.cancellation:
        changes.append(
            MaterialChange(
                field="cancellation",
                before=str(old.cancellation),
                after=str(new.cancellation),
            )
        )

    if old.claim_supporting_texts != new.claim_supporting_texts:
        changes.append(
            MaterialChange(
                field="claim_evidence",
                before=" | ".join(old.claim_supporting_texts),
                after=" | ".join(new.claim_supporting_texts),
            )
        )

    return changes


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _format_money(minor_units: int | None, currency: str | None) -> str:
    if minor_units is None:
        return ""
    return f"{currency or 'CNY'} {minor_units}"
