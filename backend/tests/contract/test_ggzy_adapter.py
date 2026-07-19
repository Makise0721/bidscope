"""Contract tests for the GGZY curated-excerpt snapshot adapter.

Parses the single publicly verified construction-tender fixture and asserts the
output matches the human-reviewed ``expected.json``. GGZY uses a different
page structure (``ul.info`` list) from CCGP (``table.table``), confirming the
two adapters own separate, testable parsing rules.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from bidscope.snapshots import _parse
from bidscope.snapshots.ggzy import GgzySnapshotAdapter


def test_ggzy_fixture_matches_human_reviewed_record(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ggzy/2026-07-18-construction"
    adapter = GgzySnapshotAdapter()

    actual = adapter.parse(bundle)
    expected = adapter.load_expected(bundle)

    assert [item.model_dump(mode="json") for item in actual] == expected


def test_ggzy_uses_verified_official_url(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ggzy/2026-07-18-construction"
    adapter = GgzySnapshotAdapter()

    (notice,) = adapter.parse(bundle)

    assert notice.source.value == "ggzy"
    assert notice.source_url.host == "www.ggzy.gov.cn"
    assert notice.capture_kind.value == "curated_public_excerpt"


def test_ggzy_parses_curated_excerpt_fields(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ggzy/2026-07-18-construction"
    adapter = GgzySnapshotAdapter()

    (notice,) = adapter.parse(bundle)

    assert notice.title == "重庆市智算中心服务器及存储设备采购公开招标公告"
    assert notice.purchaser == "重庆市大数据应用发展管理局"
    assert notice.region == "重庆市"
    assert notice.publish_time is not None
    assert notice.publish_time.tzinfo is not None
    assert notice.deadline is not None
    assert notice.deadline.tzinfo is not None
    assert notice.budget is not None
    assert notice.budget.minor_units == 530_000_000
    assert notice.budget.raw_text == "人民币530万元"
    assert notice.parser_version == "ggzy-v1"


def test_ggzy_drift_when_title_element_missing(
    project_root: Path, tmp_path: Path
) -> None:
    """Removing the required title element must raise a typed ParseDrift."""
    src = project_root / "data/snapshots/ggzy/2026-07-18-construction"

    for name in ("detail.html", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    detail = (tmp_path / "detail.html").read_text(encoding="utf-8")
    # Change the selector the GGZY adapter relies on (div.title h2 -> div.typo h2).
    (tmp_path / "detail.html").write_text(
        detail.replace('<div class="title">', '<div class="typo">'), encoding="utf-8"
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((tmp_path / "detail.html").read_bytes()).hexdigest()
    manifest["files"]["detail.html"] = digest
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    adapter = GgzySnapshotAdapter()
    with pytest.raises(_parse.ParseDrift) as exc_info:
        adapter.parse(tmp_path)

    assert exc_info.value.path == "detail.html:div.title h2"
