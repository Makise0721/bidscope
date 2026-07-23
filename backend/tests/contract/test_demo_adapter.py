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
    """A record with a non-synthetic URL is skipped; the rest still parse.

    The bad record (demo-001 with an official host) must not appear in the
    parsed output, but the other 11 records are still imported — partial-source
    resilience per design §9.
    """
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"url": "https://www.ccgp.gov.cn/demo-001"})

    adapter = DemoSnapshotAdapter()
    notices = adapter.parse(tmp_path)

    # The bad record is excluded; the other 11 survive.
    ids = {n.external_id for n in notices}
    assert "demo-001" not in ids, "record with non-synthetic URL was not skipped"
    assert len(notices) == 11, f"expected 11 good records, got {len(notices)}"


def test_demo_rejects_non_demo_id(project_root: Path, tmp_path: Path) -> None:
    """A record whose external_id lacks the ``demo-`` prefix is skipped."""
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"id": "off-001", "url": "https://example.invalid/off-001"})

    adapter = DemoSnapshotAdapter()
    notices = adapter.parse(tmp_path)

    ids = {n.external_id for n in notices}
    assert "off-001" not in ids, "record with non-demo id was not skipped"
    assert "demo-001" not in ids, "original demo-001 should be replaced by the patched record"
    assert len(notices) == 11


def test_demo_rejects_official_source_spoof(project_root: Path, tmp_path: Path) -> None:
    """Source spoofing (official URL on a synthetic_demo record) is skipped."""
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"url": "https://www.ggzy.gov.cn/demo-001"})

    adapter = DemoSnapshotAdapter()
    notices = adapter.parse(tmp_path)

    ids = {n.external_id for n in notices}
    assert "demo-001" not in ids, "spoofed record was not skipped"
    assert len(notices) == 11


def test_demo_rejects_naive_datetime(project_root: Path, tmp_path: Path) -> None:
    """A record with a timezone-naive datetime is skipped."""
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    _patch_record(tmp_path, "demo-001", {"publish_time": "2026-07-15T09:00:00"})

    adapter = DemoSnapshotAdapter()
    notices = adapter.parse(tmp_path)

    ids = {n.external_id for n in notices}
    assert "demo-001" not in ids, "record with naive datetime was not skipped"
    assert len(notices) == 11


def test_demo_all_records_bad_raises_parse_drift(project_root: Path, tmp_path: Path) -> None:
    """When EVERY record is unparseable, ``parse`` raises ``ParseDrift``.

    This is the all-or-nothing boundary: partial import only applies when at
    least one record is valid. A fully-corrupt bundle fails loudly.
    """
    src = project_root / "data/demo/batch-1"
    for name in ("notices.json", "expected.json", "manifest.json"):
        shutil.copy2(src / name, tmp_path / name)
    # Patch ALL records to have an empty URL (invalid HttpUrl).
    payload = json.loads((tmp_path / "notices.json").read_text(encoding="utf-8"))
    for record in payload["notices"]:
        record["url"] = ""
    (tmp_path / "notices.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((tmp_path / "notices.json").read_bytes()).hexdigest()
    manifest["files"]["notices.json"] = digest
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    adapter = DemoSnapshotAdapter()
    from bidscope.snapshots._parse import ParseDrift

    with pytest.raises(ParseDrift):
        adapter.parse(tmp_path)


def test_demo_preserves_synthetic_channel_in_raw_fields(project_root: Path) -> None:
    bundle = project_root / "data/demo/batch-1"
    adapter = DemoSnapshotAdapter()

    by_id = {n.external_id: n for n in adapter.parse(bundle)}
    assert by_id["demo-001"].raw_fields.get("synthetic_channel") == "channel_a"
    assert by_id["demo-004"].raw_fields.get("synthetic_channel") == "channel_b"
