"""Structural tests for the DeepSeek semantic verifier wire sanitization.

No network calls: the structured LLM output is simulated, and
:func:`~bidscope.evidence.deepseek_verifier._sanitize` is asserted to repair
anything that violates the Semantic Citation Contract §4 invariants.
"""

from __future__ import annotations

from bidscope.domain.enums import ClaimSupportStatus
from bidscope.evidence.deepseek_verifier import (
    ClaimSupportVerificationWire,
    _sanitize,
)


def test_sanitize_drops_out_of_scope_ids() -> None:
    """Model-referenced ids outside the claim's citation set are removed."""
    wire = ClaimSupportVerificationWire(
        status="unsupported",
        rationale="金额冲突",
        evidence_ids_used=["ev-0", "hacker-id"],
        conflict_evidence_ids=["ev-0", "hacker-id"],
    )
    result = _sanitize(wire, allowed_ids={"ev-0"}, version="deepseek-chat-semantic-v1")
    assert result.status == ClaimSupportStatus.UNSUPPORTED
    assert result.evidence_ids_used == ("ev-0",)
    assert result.conflict_evidence_ids == ("ev-0",)
    assert result.verifier_version == "deepseek-chat-semantic-v1"


def test_sanitize_empties_conflict_for_non_unsupported() -> None:
    """A SUPPORTED verdict with a stray conflict list is repaired to empty."""
    wire = ClaimSupportVerificationWire(
        status="supported",
        rationale="金额一致",
        evidence_ids_used=["ev-0"],
        conflict_evidence_ids=["ev-0"],
    )
    result = _sanitize(wire, allowed_ids={"ev-0"}, version="v1")
    assert result.status == ClaimSupportStatus.SUPPORTED
    assert result.conflict_evidence_ids == ()


def test_sanitize_falls_back_for_blank_rationale() -> None:
    """A blank rationale would violate the non-empty invariant; keep a stub."""
    wire = ClaimSupportVerificationWire(
        status="uncertain", rationale="   ", evidence_ids_used=[], conflict_evidence_ids=[]
    )
    result = _sanitize(wire, allowed_ids=set(), version="v1")
    assert result.status == ClaimSupportStatus.UNCERTAIN
    assert result.rationale.strip()
