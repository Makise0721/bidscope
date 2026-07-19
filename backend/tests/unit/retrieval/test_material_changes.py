"""Unit tests for material-change detection between two notice versions.

A material change touches one of the configured business fields (deadline,
budget, region, purchaser, procurement scope, cancellation state, or the
source text backing a reported claim). Formatting-only differences — leading or
trailing whitespace, consecutive whitespace, Unicode common equivalence, and
punctuation/formatting that does not alter meaning — are not material. The
module is a pure function that never depends on the current time and returns
changes in a stable field order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bidscope.retrieval.deduplication import (
    MaterialChange,
    NoticeView,
    detect_material_changes,
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


def _fields(changes: list[MaterialChange]) -> list[str]:
    return [change.field for change in changes]


class TestMaterialFields:
    """Each material field change is reported."""

    def test_deadline_change(self) -> None:
        old = _notice(deadline=datetime(2026, 8, 1, 9, 0, tzinfo=UTC))
        new = _notice(deadline=datetime(2026, 8, 10, 9, 0, tzinfo=UTC))

        changes = detect_material_changes(old, new)

        assert "deadline" in _fields(changes)

    def test_budget_change(self) -> None:
        old = _notice(budget_minor_units=680_000_000)
        new = _notice(budget_minor_units=920_000_000)

        changes = detect_material_changes(old, new)

        assert "budget" in _fields(changes)

    def test_region_change(self) -> None:
        old = _notice(region="四川省")
        new = _notice(region="重庆市")

        changes = detect_material_changes(old, new)

        assert "region" in _fields(changes)

    def test_purchaser_change(self) -> None:
        old = _notice(purchaser="四川省大数据中心")
        new = _notice(purchaser="重庆市大数据局")

        changes = detect_material_changes(old, new)

        assert "purchaser" in _fields(changes)

    def test_procurement_scope_change(self) -> None:
        old = _notice(procurement_scope="智算中心服务器")
        new = _notice(procurement_scope="智算中心存储与服务器")

        changes = detect_material_changes(old, new)

        assert "scope" in _fields(changes)

    def test_cancellation_change(self) -> None:
        old = _notice(cancellation=False)
        new = _notice(cancellation=True)

        changes = detect_material_changes(old, new)

        assert "cancellation" in _fields(changes)

    def test_claim_supporting_evidence_change(self) -> None:
        old = _notice(claim_supporting_texts=("预算680万元",))
        new = _notice(claim_supporting_texts=("预算920万元",))

        changes = detect_material_changes(old, new)

        assert "claim_evidence" in _fields(changes)


class TestFormattingOnlyChanges:
    """Formatting-only differences are not material."""

    def test_title_whitespace_only_change_is_not_reported(self) -> None:
        old = _notice(title="四川省智算中心服务器采购")
        new = _notice(title="  四川省智算中心服务器采购  ")

        changes = detect_material_changes(old, new)

        assert changes == [], "title is not a material field; no change should be reported"

    def test_purchaser_whitespace_only_change_is_not_reported(self) -> None:
        old = _notice(purchaser="四川省大数据中心")
        new = _notice(purchaser="  四川省大数据中心  ")

        changes = detect_material_changes(old, new)

        assert "purchaser" not in _fields(changes)

    def test_unicode_equivalence_is_not_material(self) -> None:
        # "Equivalent" forms that normalize to the same text under NFKC.
        old = _notice(region="四川ﬁ")  # fi ligature
        new = _notice(region="四川fi")

        changes = detect_material_changes(old, new)

        assert "region" not in _fields(changes)

    def test_irrelevant_raw_fields_change_is_not_reported(self) -> None:
        # raw_fields are deliberately outside the material-field set.
        old = _notice(external_id="demo-1")
        new = _notice(external_id="demo-2")

        changes = detect_material_changes(old, new)

        assert changes == [], "external_id/raw metadata changes are not material"


class TestPresentationContract:
    """Stable output ordering and value explainability."""

    def test_field_order_is_stable(self) -> None:
        old = _notice()
        new = _notice(
            deadline=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
            budget_minor_units=920_000_000,
            region="重庆市",
        )

        changes = detect_material_changes(old, new)

        assert _fields(changes) == ["deadline", "budget", "region"]

    def test_change_values_are_explainable(self) -> None:
        old = _notice(budget_minor_units=680_000_000, deadline=datetime(2026, 8, 1, tzinfo=UTC))
        new = _notice(budget_minor_units=920_000_000, deadline=datetime(2026, 8, 10, tzinfo=UTC))

        changes = detect_material_changes(old, new)

        for change in changes:
            assert isinstance(change, MaterialChange)
            assert change.field in {
                "deadline",
                "budget",
                "region",
                "purchaser",
                "scope",
                "cancellation",
                "claim_evidence",
            }
            assert change.before is not None
            assert change.after is not None

    def test_no_changes_returns_empty_list(self) -> None:
        notice = _notice()

        assert detect_material_changes(notice, notice) == []
