import hashlib
import json
from pathlib import Path

import pytest
from bidscope.snapshots.adapters import inspect_bundle


def _write_bundle(tmp_path: Path, files: dict[str, str], manifest: dict) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, content in files.items():
        (bundle / name).write_text(content, encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _manifest(payload_files: dict[str, str] | None = None, **overrides: object) -> dict:
    payload_files = payload_files if payload_files is not None else {}
    file_hashes = overrides.pop("files", {})
    return {
        "schema_version": 1,
        "bundle_id": "ccgp-central-20260718",
        "source": "ccgp",
        "capture_kind": "curated_public_excerpt",
        "source_urls": ["https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/example.htm"],
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "retrieval_outcome": "waf_blocked_after_public_verification",
        "parser_version": "ccgp-v1",
        "files": {
            **{
                name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for name, content in payload_files.items()
            },
            **file_hashes,
        },
        **overrides,
    }


def _authorized_contract(*, review_status: str = "approved") -> dict[str, object]:
    return {
        "contract_version": "ccgp-curated-v1",
        "authorization_ref": "pilot-ccgp-20260729",
        "data_owner": "internal-pilot-owner",
        "regions": ["全国"],
        "categories": ["central-public-tender"],
        "review_status": review_status,
        "reviewed_at": "2026-07-29T09:00:00+00:00",
        "update_sla": "weekly",
        "retention_days": 365,
    }


def test_inspect_bundle_rejects_modified_file(tmp_path: Path) -> None:
    files = {"detail.html": "<html>original</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest(files))
    (bundle / "detail.html").write_text("changed", encoding="utf-8")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "snapshot_integrity_error" for error in inspection.errors)


def test_schema_v2_requires_authorized_data_contract(tmp_path: Path) -> None:
    files = {"detail.html": "<html>authorized</html>"}
    manifest = _manifest(files, schema_version=2, batch_id="ccgp-batch-20260729")
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert inspection.manifest_sha256 == hashlib.sha256(
        (bundle / "manifest.json").read_bytes()
    ).hexdigest()
    assert any(error.code == "missing_data_contract" for error in inspection.errors)


def test_schema_v2_accepts_approved_authorized_contract(tmp_path: Path) -> None:
    files = {"detail.html": "<html>authorized</html>"}
    manifest = _manifest(
        files,
        schema_version=2,
        batch_id="ccgp-batch-20260729",
        data_contract=_authorized_contract(),
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is True
    assert inspection.disposition == "accepted"
    assert inspection.manifest is not None
    assert inspection.manifest.batch_id == "ccgp-batch-20260729"
    assert inspection.manifest.data_contract is not None
    assert inspection.manifest.data_contract.authorization_ref == "pilot-ccgp-20260729"


def test_schema_v2_quarantines_unapproved_contract(tmp_path: Path) -> None:
    files = {"detail.html": "<html>pending</html>"}
    manifest = _manifest(
        files,
        schema_version=2,
        batch_id="ccgp-batch-20260729",
        data_contract=_authorized_contract(review_status="pending"),
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert any(error.code == "authorization_not_approved" for error in inspection.errors)


def test_unknown_schema_version_is_quarantined(tmp_path: Path) -> None:
    files = {"detail.html": "<html>unknown schema</html>"}
    manifest = _manifest(
        files,
        schema_version=3,
        batch_id="ccgp-batch-20260729",
        data_contract=_authorized_contract(),
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert any(error.code == "invalid_manifest_field" for error in inspection.errors)


def test_schema_v2_rejects_batch_id_path_separators(tmp_path: Path) -> None:
    files = {"detail.html": "<html>invalid batch</html>"}
    manifest = _manifest(
        files,
        schema_version=2,
        batch_id="../ccgp-batch-20260729",
        data_contract=_authorized_contract(),
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert any(error.code == "invalid_manifest_field" for error in inspection.errors)


def test_schema_v2_requires_review_timestamp_for_approved_contract(tmp_path: Path) -> None:
    files = {"detail.html": "<html>unreviewed</html>"}
    contract = _authorized_contract()
    contract.pop("reviewed_at")
    manifest = _manifest(
        files,
        schema_version=2,
        batch_id="ccgp-batch-20260729",
        data_contract=contract,
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert any(error.code == "invalid_manifest_field" for error in inspection.errors)


def test_fixture_hash_is_sha256(tmp_path: Path) -> None:
    files = {"detail.html": "<html>hello</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest(files))

    payload = (bundle / "detail.html").read_bytes()
    inspection = inspect_bundle(bundle)

    assert inspection.valid is True
    assert inspection.actual_hashes["detail.html"] == hashlib.sha256(payload).hexdigest()
    assert inspection.manifest_sha256 == hashlib.sha256(
        (bundle / "manifest.json").read_bytes()
    ).hexdigest()


def test_invalid_manifest_encoding_is_quarantined(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(b"{\xff")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert inspection.errors[0].code == "invalid_manifest"


def test_manifest_symlink_is_quarantined(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    target = tmp_path / "manifest-target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (bundle / "manifest.json").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert inspection.disposition == "quarantined"
    assert inspection.errors[0].code == "invalid_file_type"


def test_inspect_bundle_rejects_undeclared_file(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest(files))
    (bundle / "extra.html").write_text("undeclared", encoding="utf-8")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "unexpected_file" for error in inspection.errors)


def test_inspect_bundle_rejects_missing_declared_file(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(files)
    bundle = _write_bundle(tmp_path, {}, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "missing_file" for error in inspection.errors)


def test_inspect_bundle_rejects_traversal_path_in_manifest(tmp_path: Path) -> None:
    payload = {"detail.html": "<html>x</html>"}
    # Path traversal: declared file would resolve outside the bundle dir.
    manifest = _manifest(payload, files={"../escape.html": "a" * 64})
    bundle = _write_bundle(tmp_path, payload, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_path" for error in inspection.errors)


def test_inspect_bundle_rejects_non_https_official_source(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(
        files,
        source_urls=["http://www.ccgp.gov.cn/insecure.htm"],
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_source_url" for error in inspection.errors)


def test_inspect_bundle_rejects_official_host_for_synthetic(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(
        files,
        capture_kind="synthetic_demo",
        source="synthetic_demo",
        source_urls=["https://www.ccgp.gov.cn/demo.htm"],
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_source_url" for error in inspection.errors)


def test_inspect_bundle_requires_synthetic_source_for_synthetic_kind(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(
        files,
        capture_kind="synthetic_demo",
        source="ccgp",
        source_urls=["https://example.invalid/demo.htm"],
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "source_capture_mismatch" for error in inspection.errors)


def test_inspect_bundle_accepts_valid_synthetic_bundle(tmp_path: Path) -> None:
    files = {"detail.html": "<html>demo</html>"}
    manifest = _manifest(
        files,
        bundle_id="demo-batch-1",
        capture_kind="synthetic_demo",
        source="synthetic_demo",
        source_urls=["https://example.invalid/demo.htm"],
        retrieval_outcome="n/a",
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is True
    assert inspection.bundle_id == "demo-batch-1"
    assert inspection.errors == []


# --- Regression tests for the gaps the review surfaced ---


def test_inspect_bundle_rejects_unknown_capture_kind(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(files, capture_kind="bogus_kind")
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_enum_value" for error in inspection.errors)


def test_inspect_bundle_rejects_synthetic_source_with_official_kind(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(
        files,
        source="synthetic_demo",
        source_urls=["https://www.ccgp.gov.cn/a.htm"],
    )
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "source_capture_mismatch" for error in inspection.errors)


def test_inspect_bundle_handles_none_source_urls(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(files, source_urls=None)
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    # Must produce a typed error, NOT raise TypeError.
    assert inspection.valid is False
    assert inspection.errors, "expected structured errors for None source_urls"


def test_inspect_bundle_rejects_empty_files(tmp_path: Path) -> None:
    manifest = _manifest(files={})
    bundle = _write_bundle(tmp_path, {}, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_manifest_field" for error in inspection.errors)


def test_inspect_bundle_rejects_invalid_sha256_format(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(files, files={"detail.html": "not-a-hash"})
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_file_hash" for error in inspection.errors)


def test_inspect_bundle_rejects_non_object_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_manifest" for error in inspection.errors)



def test_inspect_bundle_rejects_directory_as_payload(tmp_path: Path) -> None:
    """Declaring a directory (e.g. ".") as a payload must be rejected."""
    import hashlib

    payload = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(tmp_path, payload, _manifest(payload))
    dir_hash = hashlib.sha256(b"").hexdigest()
    manifest = _manifest(payload, files={".": dir_hash})
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_file_type" for error in inspection.errors)


def test_inspect_bundle_rejects_symlink_as_payload(tmp_path: Path) -> None:
    """A symlink declared as a payload must be rejected as invalid_file_type."""
    import hashlib
    import sys

    payload = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(tmp_path, payload, _manifest(payload))
    target = tmp_path / "real-target.txt"
    target.write_text("target", encoding="utf-8")
    link = bundle / "link.html"
    try:
        link.symlink_to(target)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("symlink creation requires privileges on Windows")
        raise

    link_hash = hashlib.sha256(b"target").hexdigest()
    manifest = _manifest(payload, files={"link.html": link_hash})
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_file_type" for error in inspection.errors)


def test_inspect_bundle_rejects_empty_bundle_id(tmp_path: Path) -> None:
    files = {"detail.html": "<html>x</html>"}
    manifest = _manifest(files, bundle_id="")
    bundle = _write_bundle(tmp_path, files, manifest)

    inspection = inspect_bundle(bundle)

    assert inspection.valid is False
    assert any(error.code == "invalid_manifest_field" for error in inspection.errors)
