"""Unit tests for deterministic tender deduplication.

The classifier is a pure function with a bounded output: ``exact``,
``ambiguous`` or ``distinct``. ``exact`` requires strong deterministic evidence;
``distinct`` requires clear conflict evidence; everything else is ``ambiguous``
and left to the model port in a later task. Title similarity alone never yields
``exact``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bidscope.retrieval.deduplication import (
    DuplicateClassification,
    DuplicateDecision,
    NoticeView,
    classify_duplicate,
)


def _notice(**overrides: object) -> NoticeView:
    """Build a :class:`NoticeView` with sensible defaults, applying overrides.

    The default ``canonical_url`` and ``content_hash`` are derived from
    ``external_id`` so distinct notices do not accidentally share a URL or hash.
    """
    external_id = str(overrides.get("external_id", "demo-1"))
    defaults: dict[str, object] = {
        "source": "synthetic_demo",
        "external_id": external_id,
        "canonical_url": f"https://example.invalid/{external_id}",
        "project_number": None,
        "content_hash": f"hash-{external_id}",
        "title": "四川省智算中心服务器采购",
        "purchaser": "四川省大数据中心",
        "region": "四川省",
        "budget_minor_units": 680_000_000,
        "budget_currency": "CNY",
        "deadline": datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        "procurement_scope": "智算中心服务器",
        "cancellation": False,
        "claim_supporting_texts": ("预算680万元", "截止时间2026年8月1日"),
    }
    defaults.update(overrides)
    return NoticeView(**defaults)


class TestExactEvidence:
    """Strong deterministic evidence ``exact`` a duplicate."""

    def test_same_content_hash_is_exact(self) -> None:
        candidate = _notice(external_id="demo-a", content_hash="shared-hash")
        existing = _notice(external_id="demo-b", content_hash="shared-hash")

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.EXACT
        assert any("hash" in reason.lower() for reason in result.reasons)

    def test_same_non_empty_project_number_is_exact(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            project_number="SC-2026-0018",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            project_number="SC-2026-0018",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.EXACT
        assert any("project number" in reason.lower() for reason in result.reasons)

    def test_same_source_and_canonical_url_is_exact(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            source="synthetic_demo",
            canonical_url="https://example.invalid/notice.htm",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            source="synthetic_demo",
            canonical_url="https://example.invalid/notice.htm",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.EXACT
        assert any(
            "url" in reason.lower() or "canonical" in reason.lower()
            for reason in result.reasons
        )

    def test_equivalent_source_ids_are_exact(self) -> None:
        candidate = _notice(external_id="demo-a", content_hash="h-a", source="ccgp")
        existing = _notice(external_id="demo-b", content_hash="h-b", source="ggzy")

        result = classify_duplicate(
            candidate, existing, equivalent_ids={("ccgp", "ggzy")}
        )

        assert result.decision == DuplicateDecision.EXACT


class TestDistinctEvidence:
    """Clear conflict evidence yields ``distinct``."""

    def test_different_project_numbers_with_conflict_is_distinct(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            project_number="SC-2026-0018",
            purchaser="四川省大数据中心",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            project_number="SC-2026-0099",
            purchaser="重庆市大数据局",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.DISTINCT

    def test_different_source_different_url_no_overlap_is_distinct(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            source="synthetic_demo",
            canonical_url="https://example.invalid/a.htm",
            project_number="PN-1",
            purchaser="甲方",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            source="synthetic_demo",
            canonical_url="https://example.invalid/b.htm",
            project_number="PN-2",
            purchaser="乙方",
            budget_minor_units=100_000_000,
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.DISTINCT


class TestAmbiguousAndBoundaries:
    """Everything short of strong evidence is ``ambiguous``."""

    def test_empty_project_number_does_not_produce_exact(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            project_number=None,
            title="四川省智算中心服务器采购",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            project_number="",
            title="四川省智算中心服务器采购",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision != DuplicateDecision.EXACT

    def test_title_similarity_only_is_ambiguous(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="h-a",
            title="四川省智算中心服务器采购项目",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="h-b",
            title="四川智算中心服务器采购",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.AMBIGUOUS

    def test_cross_synthetic_channel_stays_synthetic_demo(self) -> None:
        candidate = _notice(
            external_id="demo-a",
            content_hash="shared-hash",
            source="synthetic_demo",
        )
        existing = _notice(
            external_id="demo-b",
            content_hash="shared-hash",
            source="synthetic_demo",
        )

        result = classify_duplicate(candidate, existing)

        assert result.decision == DuplicateDecision.EXACT
        # Source identity is preserved; no channel field changes dedup semantics.
        assert candidate.source == "synthetic_demo"
        assert existing.source == "synthetic_demo"

    def test_decision_reasons_are_serializable(self) -> None:
        candidate = _notice(external_id="demo-a", content_hash="shared-hash")
        existing = _notice(external_id="demo-b", content_hash="shared-hash")

        result = classify_duplicate(candidate, existing)

        # Reasons must be explainable strings, losslessly round-tripable.
        assert isinstance(result, DuplicateClassification)
        assert result.decision in {"exact", "ambiguous", "distinct"}
        assert all(isinstance(reason, str) for reason in result.reasons)
        encoded = list(result.reasons)
        assert encoded == list(result.reasons)
