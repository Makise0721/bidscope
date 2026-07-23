"""Security boundary tests for snapshot manifests and bundle inspection.

These tests document the security boundaries enforced by the snapshot import
pipeline: HTTPS enforcement, host allowlists, path traversal prevention, hash
integrity checks, and provenance validation. Tests against existing guards
should PASS (proving the guard works); tests against missing guards should
FAIL (exposing a gap).

Each test is offline — no network, no real API keys.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

import pytest
from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.provenance import (
    SYNTHETIC_HOST,
    validate_provenance,
)
from bidscope.domain.snapshots import SnapshotManifest
from bidscope.snapshots.adapters import inspect_bundle
from pydantic import HttpUrl, ValidationError

# --- helpers -----------------------------------------------------------------


def _valid_manifest_dict() -> dict:
    """Return a minimal valid manifest dict that passes all guards.

    The ``retrieved_at`` field is a JSON-serialisable ISO string (not a
    datetime object) so the manifest can be written to manifest.json via
    ``json.dumps`` without a custom encoder. ``SnapshotManifest`` parses
    the ISO string back to a timezone-aware datetime at validation time.
    """
    return {
        "schema_version": 1,
        "bundle_id": "ccgp-central-20260718",
        "source": SourceName.CCGP,
        "capture_kind": CaptureKind.CURATED_PUBLIC_EXCERPT,
        "source_urls": ["https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/example.htm"],
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "retrieval_outcome": "ok",
        "parser_version": "ccgp-v1",
        "files": {"detail.html": "a" * 64},
    }


def _write_bundle(
    tmp_path: Path,
    files: dict[str, str],
    manifest: dict,
) -> Path:
    """Write a bundle directory with payload files and a manifest.json."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, content in files.items():
        (bundle / name).write_text(content, encoding="utf-8")
    (bundle / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return bundle


def _manifest_with_files(payload_files: dict[str, str]) -> dict:
    """Build a manifest whose ``files`` map has correct SHA-256 hashes."""
    return {
        **_valid_manifest_dict(),
        "files": {
            name: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for name, content in payload_files.items()
        },
    }


# 1. HTTPS rejection ----------------------------------------------------------


def test_manifest_rejects_http_scheme() -> None:
    """SnapshotManifest must reject source_urls that use HTTP (non-TLS)."""
    data = _valid_manifest_dict()
    data["source_urls"] = ["http://www.ccgp.gov.cn/a.htm"]
    with pytest.raises(ValidationError) as exc_info:
        SnapshotManifest.model_validate(data)
    assert "https" in str(exc_info.value).lower()


def test_manifest_rejects_ftp_scheme() -> None:
    """SnapshotManifest must reject source_urls that use FTP."""
    data = _valid_manifest_dict()
    data["source_urls"] = ["ftp://files.example.com/a.htm"]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)


# 2. Lookalike host rejection ------------------------------------------------


def test_manifest_rejects_lookalike_host() -> None:
    """A URL whose host is a subdomain lookalike of a real source must fail.

    ``www.ccgp.gov.cn.evil.com`` has host ``www.ccgp.gov.cn.evil.com``, which
    is NOT in the CCGP allowlist. This guards against typosquatting attacks.
    """
    data = _valid_manifest_dict()
    data["source_urls"] = ["https://www.ccgp.gov.cn.evil.com/a.htm"]
    with pytest.raises(ValidationError) as exc_info:
        SnapshotManifest.model_validate(data)
    error_msg = str(exc_info.value)
    # Must mention that the host is not allowed for this source
    assert "ccgp.gov.cn" in error_msg or "may only reference" in error_msg


def test_manifest_rejects_lookalike_ggzy_host() -> None:
    """Same lookalike guard for the GGZY source."""
    data = _valid_manifest_dict()
    data["source"] = SourceName.GGZY
    data["source_urls"] = ["https://www.ggzy.gov.cn.phishing.net/a.htm"]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)


# 3. Userinfo URL rejection --------------------------------------------------


@pytest.mark.parametrize(
    "source_url",
    [
        "https://user:secret@www.ccgp.gov.cn/cggg/detail.htm",
        "https://@www.ccgp.gov.cn/cggg/detail.htm",
        "https://:@www.ccgp.gov.cn/cggg/detail.htm",
        "https://www.ccgp.gov.cn:8443/cggg/detail.htm",
    ],
)
def test_manifest_rejects_url_credentials_and_non_default_port(source_url: str) -> None:
    """Source URLs must not carry credentials or an unexpected explicit port."""
    data = _valid_manifest_dict()
    data["source_urls"] = [source_url]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)


def test_manifest_accepts_ordinary_approved_https_url() -> None:
    """An approved HTTPS URL without credentials or a custom port remains valid."""
    manifest = SnapshotManifest.model_validate(_valid_manifest_dict())
    assert manifest.source_urls[0].host == "www.ccgp.gov.cn"


@pytest.mark.parametrize(
    "source_urls",
    [
        ["https://www.ccgp.gov.cn/cggg/detail.htm"],
        ("https://www.ccgp.gov.cn/cggg/detail.htm",),
        {"https://www.ccgp.gov.cn/cggg/detail.htm"},
        frozenset({"https://www.ccgp.gov.cn/cggg/detail.htm"}),
        deque(["https://www.ccgp.gov.cn/cggg/detail.htm"]),
    ],
    ids=["list", "tuple", "set", "frozenset", "deque"],
)
def test_manifest_accepts_exact_raw_string_url_collections(source_urls: object) -> None:
    """Supported raw-string collection types are normalized to a plain list."""
    data = _valid_manifest_dict()
    data["source_urls"] = source_urls

    manifest = SnapshotManifest.model_validate(data)

    assert isinstance(manifest.source_urls, list)
    assert manifest.source_urls[0].host == "www.ccgp.gov.cn"


def test_manifest_rejects_list_subclass_before_url_normalization() -> None:
    """A list subclass cannot bypass raw userinfo inspection."""

    class UrlList(list[str]):
        pass

    data = _valid_manifest_dict()
    data["source_urls"] = UrlList(["https://@www.ccgp.gov.cn/cggg/detail.htm"])

    with pytest.raises(ValidationError, match="source_urls"):
        SnapshotManifest.model_validate(data)


def test_manifest_rejects_generator_before_url_normalization() -> None:
    """A generator cannot bypass raw userinfo inspection."""
    data = _valid_manifest_dict()
    data["source_urls"] = ("https://@www.ccgp.gov.cn/cggg/detail.htm" for _ in range(1))

    with pytest.raises(ValidationError, match="source_urls"):
        SnapshotManifest.model_validate(data)


def test_manifest_rejects_prebuilt_http_url_before_url_normalization() -> None:
    """Typed URLs are not valid raw provenance manifest entries."""
    data = _valid_manifest_dict()
    data["source_urls"] = [HttpUrl("https://@www.ccgp.gov.cn/cggg/detail.htm")]

    with pytest.raises(ValidationError, match="source_urls"):
        SnapshotManifest.model_validate(data)


def test_manifest_rejects_broken_custom_iterable_as_validation_error() -> None:
    """A custom iterable violates the raw-string manifest input contract."""

    class BrokenIterable:
        def __iter__(self) -> object:
            raise TypeError("custom iterator must not be consumed")

    data = _valid_manifest_dict()
    data["source_urls"] = BrokenIterable()

    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)


# 4. Non-default port ---------------------------------------------------------


def test_manifest_accepts_https_default_port_explicit() -> None:
    """Explicit port 443 on an allowed host must still pass provenance.

    Pydantic normalises ``https://host:443/`` to ``https://host/``, so the
    host is correctly identified as the allowed CCGP host.
    """
    data = _valid_manifest_dict()
    data["source_urls"] = ["https://www.ccgp.gov.cn:443/a.htm"]
    manifest = SnapshotManifest.model_validate(data)
    # Port 443 is the default HTTPS port and is stripped by URL normalisation.
    assert manifest.source_urls[0].host == "www.ccgp.gov.cn"


def test_manifest_rejects_non_default_port() -> None:
    """A non-default HTTPS port is outside the approved provenance boundary."""
    data = _valid_manifest_dict()
    data["source_urls"] = ["https://www.ccgp.gov.cn:8443/a.htm"]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)



# 5. Path traversal in manifest files ----------------------------------------


def test_inspect_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    """A manifest declaring ``../etc/passwd`` must fail bundle inspection.

    The ``_safe_relative`` resolver must detect the ``..`` segment and
    reject the declared path as escaping the bundle directory.
    """
    payload = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(
        tmp_path,
        payload,
        {
            **_valid_manifest_dict(),
            "files": {"../etc/passwd": "a" * 64},
        },
    )
    result = inspect_bundle(bundle)
    assert result.valid is False
    assert any(error.code == "invalid_path" for error in result.errors)


def test_inspect_bundle_rejects_absolute_path(tmp_path: Path) -> None:
    """A manifest declaring an absolute path must fail inspection."""
    payload = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(
        tmp_path,
        payload,
        {
            **_valid_manifest_dict(),
            "files": {"/etc/shadow": "b" * 64},
        },
    )
    result = inspect_bundle(bundle)
    assert result.valid is False
    assert any(error.code == "invalid_path" for error in result.errors)


# 6. Changed hash detection --------------------------------------------------


def test_inspect_bundle_rejects_hash_mismatch(tmp_path: Path) -> None:
    """A file whose content does not match its declared hash must be rejected.

    This is the ``snapshot_integrity_error`` guard — any post-import
    modification of payload files is detected via SHA-256 comparison.
    """
    files = {"detail.html": "<html>original</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest_with_files(files))
    # Tamper with the file after hashing
    (bundle / "detail.html").write_text("<html>tampered</html>", encoding="utf-8")

    result = inspect_bundle(bundle)
    assert result.valid is False
    assert any(
        error.code == "snapshot_integrity_error" for error in result.errors
    )


def test_inspect_bundle_accepts_matching_hash(tmp_path: Path) -> None:
    """Sanity check: an unmodified file with a correct hash passes."""
    files = {"detail.html": "<html>original</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest_with_files(files))
    result = inspect_bundle(bundle)
    assert result.valid is True


# 7. Undeclared file detection ------------------------------------------------


def test_inspect_bundle_rejects_undeclared_file(tmp_path: Path) -> None:
    """A payload file not listed in the manifest's ``files`` must be rejected.

    This is the ``unexpected_file`` guard — it prevents an attacker from
    slipping an extra file into the bundle that wasn't integrity-checked.
    """
    files = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(tmp_path, files, _manifest_with_files(files))
    (bundle / "sneaky.js").write_text("alert(1)", encoding="utf-8")

    result = inspect_bundle(bundle)
    assert result.valid is False
    assert any(error.code == "unexpected_file" for error in result.errors)


# 8. Cross-source impersonation ----------------------------------------------


def test_manifest_rejects_ccgp_source_with_ggzy_host() -> None:
    """A CCGP notice attributed to a ggzy.gov.cn host must fail provenance.

    This prevents one official source from impersonating another.
    """
    data = _valid_manifest_dict()
    data["source_urls"] = ["https://www.ggzy.gov.cn/fake-ccgp.htm"]
    with pytest.raises(ValidationError) as exc_info:
        SnapshotManifest.model_validate(data)
    assert "ggzy.gov.cn" not in str(
        exc_info.value
    ) or "may only reference" in str(exc_info.value)


def test_manifest_rejects_ggzy_source_with_ccgp_host() -> None:
    """A GGZY notice attributed to a ccgp.gov.cn host must fail provenance."""
    data = _valid_manifest_dict()
    data["source"] = SourceName.GGZY
    data["source_urls"] = ["https://www.ccgp.gov.cn/fake-ggzy.htm"]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(data)


def test_validate_provenance_cross_source_impersonation() -> None:
    """Direct call to validate_provenance for cross-source impersonation."""
    result = validate_provenance(
        source=SourceName.CCGP,
        capture_kind=CaptureKind.CURATED_PUBLIC_EXCERPT,
        host="www.ggzy.gov.cn",
        external_id="sc-2026-001",
    )
    assert result.valid is False
    assert any("may only reference" in err for err in result.errors)


# 9. Synthetic with official host --------------------------------------------


def test_manifest_rejects_synthetic_kind_with_official_host() -> None:
    """A synthetic_demo capture_kind with a real government host must fail.

    Synthetic bundles must use only ``example.invalid`` — mixing synthetic
    capture_kind with a real host is a provenance violation.
    """
    data = _valid_manifest_dict()
    data["capture_kind"] = CaptureKind.SYNTHETIC_DEMO
    data["source"] = SourceName.SYNTHETIC_DEMO
    data["source_urls"] = ["https://www.ccgp.gov.cn/demo.htm"]
    with pytest.raises(ValidationError) as exc_info:
        SnapshotManifest.model_validate(data)
    assert "example.invalid" in str(exc_info.value)


def test_validate_provenance_synthetic_with_official_host() -> None:
    """Direct call: synthetic_demo with www.ccgp.gov.cn must fail."""
    result = validate_provenance(
        source=SourceName.SYNTHETIC_DEMO,
        capture_kind=CaptureKind.SYNTHETIC_DEMO,
        host="www.ccgp.gov.cn",
        external_id="demo-001",
    )
    assert result.valid is False
    assert any("example.invalid" in err for err in result.errors)


# 10. Official with synthetic prefix -----------------------------------------


def test_manifest_rejects_synthetic_prefix_with_official_kind() -> None:
    """An official capture_kind with a ``demo-`` prefixed external_id must fail.

    This prevents synthetic data from contaminating the official pipeline.
    """
    data = _valid_manifest_dict()
    data["bundle_id"] = "demo-001"
    with pytest.raises(ValidationError) as exc_info:
        SnapshotManifest.model_validate(data)
    assert "demo-" in str(exc_info.value)


def test_validate_provenance_official_with_synthetic_prefix() -> None:
    """Direct call: official kind with ``demo-`` external_id must fail."""
    result = validate_provenance(
        source=SourceName.CCGP,
        capture_kind=CaptureKind.CURATED_PUBLIC_EXCERPT,
        host="www.ccgp.gov.cn",
        external_id="demo-001",
    )
    assert result.valid is False
    assert any("demo-" in err for err in result.errors)


# --- additional provenance edge cases ---------------------------------------


def test_official_host_rejects_synthetic_host() -> None:
    """An official capture_kind must not use example.invalid."""
    result = validate_provenance(
        source=SourceName.CCGP,
        capture_kind=CaptureKind.RAW_RESPONSE,
        host=SYNTHETIC_HOST,
        external_id="sc-2026-001",
    )
    assert result.valid is False
    assert any("example.invalid" in err for err in result.errors)


def test_synthetic_requires_synthetic_source() -> None:
    """synthetic_demo capture_kind with an official source must fail."""
    result = validate_provenance(
        source=SourceName.CCGP,
        capture_kind=CaptureKind.SYNTHETIC_DEMO,
        host=SYNTHETIC_HOST,
        external_id="demo-001",
    )
    assert result.valid is False


def test_official_source_forbidden_for_official_kind() -> None:
    """synthetic_demo source with official capture_kind must fail."""
    result = validate_provenance(
        source=SourceName.SYNTHETIC_DEMO,
        capture_kind=CaptureKind.CURATED_PUBLIC_EXCERPT,
        host="www.ccgp.gov.cn",
        external_id="sc-2026-001",
    )
    assert result.valid is False
    assert any("synthetic_demo" in err for err in result.errors)


def test_valid_synthetic_bundle_passes() -> None:
    """A correctly-formed synthetic bundle must pass all guards."""
    result = validate_provenance(
        source=SourceName.SYNTHETIC_DEMO,
        capture_kind=CaptureKind.SYNTHETIC_DEMO,
        host=SYNTHETIC_HOST,
        external_id="demo-batch-1",
    )
    assert result.valid is True


def test_valid_official_bundle_passes() -> None:
    """A correctly-formed official CCGP bundle must pass all guards."""
    result = validate_provenance(
        source=SourceName.CCGP,
        capture_kind=CaptureKind.CURATED_PUBLIC_EXCERPT,
        host="www.ccgp.gov.cn",
        external_id="sc-2026-001",
    )
    assert result.valid is True


def test_valid_ggzy_bundle_passes() -> None:
    """A correctly-formed official GGZY bundle must pass all guards."""
    result = validate_provenance(
        source=SourceName.GGZY,
        capture_kind=CaptureKind.RAW_RESPONSE,
        host="www.ggzy.gov.cn",
        external_id="cq-2026-001",
    )
    assert result.valid is True


def test_manifest_rejects_search_ccgp_host() -> None:
    """The search.ccgp.gov.cn host is in the CCGP allowlist and must pass."""
    data = _valid_manifest_dict()
    data["source_urls"] = ["https://search.ccgp.gov.cn/something.htm"]
    manifest = SnapshotManifest.model_validate(data)
    assert manifest.source_urls[0].host == "search.ccgp.gov.cn"


def test_inspect_bundle_symlink_rejected(tmp_path: Path) -> None:
    """A symlink declared as a payload file must be rejected."""
    import sys

    payload = {"detail.html": "<html>x</html>"}
    bundle = _write_bundle(tmp_path, payload, _manifest_with_files(payload))
    target = tmp_path / "real.txt"
    target.write_text("target", encoding="utf-8")
    link = bundle / "link.html"
    try:
        link.symlink_to(target)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("symlink creation requires privileges on Windows")
        raise

    link_hash = hashlib.sha256(b"target").hexdigest()
    manifest = {
        **_valid_manifest_dict(),
        "files": {**payload, "link.html": link_hash},
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    result = inspect_bundle(bundle)
    assert result.valid is False
    assert any(error.code == "invalid_file_type" for error in result.errors)
