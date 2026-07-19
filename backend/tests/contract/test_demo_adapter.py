"""Contract tests for the synthetic-demo snapshot adapter.

The demo adapter parses normalised JSON (not source HTML). Batch 1 provides
enough records for the representative query to return matches and non-matches;
batch 2 exercises subscription semantics by contributing new, materially
changed, and unchanged notices, asserted here by stable ID.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from bidscope.snapshots.demo import DemoSnapshotAdapter


def _patch_record(
    bundle: Path, record_id: str, changes: dict[str, Any]
) -> None:
    """Mutate one record in a demo payload and recompute its manifest hash."""
    payload = json.loads((bundle / "notices.json").read_text(encoding="utf-8"))
    for record in payload["notices"]:
        if record.get("id") == record_id:
            record.update(changes)
            break
    else:
        raise AssertionError(f"record {record_id!r} not found in payload")
    (bundle / "notices.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((bundle / "notices.json").read_bytes()).hexdigest()
    manifest["files"]["notices.json"] = digest
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_demo_batch_one_matches_human_reviewed_records(project_root: Path) -> None:
    bundle = project_root / "data/demo/batch-1"
    adapter = DemoSnapshotAdapter()

    actual = adapter.parse(bundle)
    expected = adapter.load_expected(bundle)

    assert [item.model_dump(mode="json") for item in actual] == expected
    assert len(actual) >= 12


def test_demo_batch_one_is_synthetic(project_root: Path) -> None:
    bundle = project_root / "data/demo/batch-1"
    adapter = DemoSnapshotAdapter()

    notices = adapter.parse(bundle)
    assert notices, "expected at least one demo notice"
    for notice in notices:
        assert notice.source.value == "synthetic_demo"
        assert notice.capture_kind.value == "synthetic_demo"
        assert notice.external_id.startswith("demo-")
        assert notice.source_url.host == "example.invalid"


def _by_id(bundle: Path) -> dict[str, dict[str, Any]]:
    return {
        n["external_id"]: n for n in json.loads(
            (bundle / "expected.json").read_text(encoding="utf-8")
        )["records"]
    }


def test_demo_batch_two_has_new_changed_and_unchanged(project_root: Path) -> None:
    """Batch 2 must contribute new, materially changed, and unchanged IDs."""
    batch_one = _by_id(project_root / "data/demo/batch-1")
    batch_two = _by_id(project_root / "data/demo/batch-2")

    new_ids = set(batch_two) - set(batch_one)
    common_ids = set(batch_one) & set(batch_two)
    changed_ids = {id_ for id_ in common_ids if batch_one[id_] != batch_two[id_]}
    unchanged_ids = {id_ for id_ in common_ids if batch_one[id_] == batch_two[id_]}

    assert len(new_ids) >= 2, f"expected >=2 new notices, got {sorted(new_ids)}"
    assert len(changed_ids) >= 2, f"expected >=2 changed, got {sorted(changed_ids)}"
    assert len(unchanged_ids) >= 2, (
        f"expected >=2 unchanged, got {sorted(unchanged_ids)}"
    )


def test_demo_rejects_non_example_invalid_url(project_root: Path, tmp_path: Path) -> None:
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    # Manifest must keep declaring notices.json; only the payload changes.
    _patch_record(tmp_path, "demo-001", {"url": "https://www.ccgp.gov.cn/demo-001"})

    adapter = DemoSnapshotAdapter()
    with pytest.raises(Exception) as exc_info:  # provenance ValueError bubbling up
        adapter.parse(tmp_path)

    assert "example.invalid" in str(exc_info.value)


def test_demo_rejects_non_demo_id(project_root: Path, tmp_path: Path) -> None:
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"id": "off-001", "url": "https://example.invalid/off-001"})

    adapter = DemoSnapshotAdapter()
    with pytest.raises(Exception) as exc_info:
        adapter.parse(tmp_path)

    assert "demo-" in str(exc_info.value)


def test_demo_rejects_official_source_spoof(project_root: Path, tmp_path: Path) -> None:
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    # Force source spoofing via an official-style URL while staying synthetic_demo.
    _patch_record(tmp_path, "demo-001", {"url": "https://www.ggzy.gov.cn/demo-001"})

    adapter = DemoSnapshotAdapter()
    with pytest.raises(Exception) as exc_info:
        adapter.parse(tmp_path)

    assert "example.invalid" in str(exc_info.value)


def test_demo_rejects_naive_datetime(project_root: Path, tmp_path: Path) -> None:
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"publish_time": "2026-07-15T09:00:00"})

    adapter = DemoSnapshotAdapter()
    with pytest.raises(Exception) as exc_info:
        adapter.parse(tmp_path)

    assert "timezone-aware" in str(exc_info.value)


def test_demo_preserves_synthetic_channel_in_raw_fields(project_root: Path) -> None:
    bundle = project_root / "data/demo/batch-1"
    adapter = DemoSnapshotAdapter()

    by_id = {n.external_id: n for n in adapter.parse(bundle)}
    assert by_id["demo-001"].raw_fields.get("synthetic_channel") == "channel_a"
    assert by_id["demo-004"].raw_fields.get("synthetic_channel") == "channel_b"
