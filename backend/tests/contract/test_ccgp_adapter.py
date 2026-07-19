"""Contract tests for the CCGP curated-excerpt snapshot adapter.

Parses the single publicly verified central-open-tender fixture and asserts the
output matches the human-reviewed ``expected.json`` record-for-record. Also
asserts the verified source URL is honoured and that structural drift (a
missing title element) surfaces as a typed :class:`ParseDrift` diagnostic
rather than a bare ``KeyError``/``AttributeError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bidscope.snapshots import _parse
from bidscope.snapshots.ccgp import CcgpSnapshotAdapter


def test_ccgp_fixture_matches_human_reviewed_record(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ccgp/2026-07-18-central-open"
    adapter = CcgpSnapshotAdapter()

    actual = adapter.parse(bundle)
    expected = adapter.load_expected(bundle)

    assert [item.model_dump(mode="json") for item in actual] == expected


def test_ccgp_uses_verified_official_url(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ccgp/2026-07-18-central-open"
    adapter = CcgpSnapshotAdapter()

    (notice,) = adapter.parse(bundle)

    assert notice.source.value == "ccgp"
    assert notice.source_url.host == "www.ccgp.gov.cn"
    assert notice.capture_kind.value == "curated_public_excerpt"
    # The canonical detail URL is the one publicly verified during research.
    assert notice.source_url.path.endswith("26961813.htm")


def test_ccgp_parses_curated_excerpt_fields(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ccgp/2026-07-18-central-open"
    adapter = CcgpSnapshotAdapter()

    (notice,) = adapter.parse(bundle)

    assert notice.title == "四川省智算中心服务器采购项目公开招标公告"
    assert notice.purchaser == "四川省大数据中心"
    assert notice.region == "四川省"
    assert notice.publish_time is not None
    assert notice.publish_time.tzinfo is not None
    assert notice.deadline is not None
    assert notice.deadline.tzinfo is not None
    assert notice.budget is not None
    assert notice.budget.minor_units == 680_000_000
    assert notice.budget.raw_text == "人民币680万元"
    assert notice.parser_version == "ccgp-v1"


def test_ccgp_drift_when_title_element_missing(
    project_root: Path, tmp_path: Path
) -> None:
    """Removing the required title element must raise a typed ParseDrift."""
    src = project_root / "data/snapshots/ccgp/2026-07-18-central-open"
    # Copy fixture to tmp, drop the title element, rewrite manifest hash.
    import json
    import shutil

    for name in ("detail.html", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    detail = (tmp_path / "detail.html").read_text(encoding="utf-8")
    (tmp_path / "detail.html").write_text(
        detail.replace('<h2 class="title">', '<h2 class="typo">'), encoding="utf-8"
    )

    # Recompute the detail.html hash so integrity inspection still passes.
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    import hashlib

    digest = hashlib.sha256((tmp_path / "detail.html").read_bytes()).hexdigest()
    manifest["files"]["detail.html"] = digest
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    adapter = CcgpSnapshotAdapter()
    with pytest.raises(_parse.ParseDrift) as exc_info:
        adapter.parse(tmp_path)

    assert exc_info.value.path == "detail.html:h2.title"
