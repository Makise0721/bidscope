import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bidscope.domain.enums import CaptureKind

OFFICIAL_HOSTS = {"www.ccgp.gov.cn", "search.ccgp.gov.cn", "www.ggzy.gov.cn"}
SYNTHETIC_HOST = "example.invalid"


@dataclass
class InspectionError:
    code: str
    message: str
    path: str | None = None


@dataclass
class InspectionResult:
    valid: bool
    bundle_id: str | None = None
    errors: list[InspectionError] = field(default_factory=list)
    actual_hashes: dict[str, str] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(base: Path, candidate: str) -> Path | None:
    """Return the resolved path for a declared file, or None if it escapes base.

    Rejects path traversal that would land outside the bundle directory.
    """
    if ".." in candidate.split("/") or candidate.startswith("/"):
        return None
    resolved = (base / candidate).resolve()
    if base.resolve() != resolved and base.resolve() not in resolved.parents:
        return None
    return resolved


def _validate_source_policy(manifest: dict[str, Any], errors: list[InspectionError]) -> None:
    capture_kind = manifest.get("capture_kind")
    source = manifest.get("source")
    for url in manifest.get("source_urls", []):
        if not url.startswith("https://"):
            errors.append(
                InspectionError("invalid_source_url", f"source URL must use HTTPS: {url}", url)
            )
            continue
        host = url.split("/", 3)[2].split("@")[-1].split(":")[0]
        if capture_kind == CaptureKind.SYNTHETIC_DEMO:
            if host != SYNTHETIC_HOST:
                errors.append(
                    InspectionError(
                        "invalid_source_url",
                        f"synthetic_demo URLs must use {SYNTHETIC_HOST}: {url}",
                        url,
                    )
                )
        else:
            if host not in OFFICIAL_HOSTS:
                errors.append(
                    InspectionError(
                        "invalid_source_url",
                        f"official bundles may only reference {sorted(OFFICIAL_HOSTS)}: {url}",
                        url,
                    )
                )

    if capture_kind == CaptureKind.SYNTHETIC_DEMO and source != "synthetic_demo":
        errors.append(
            InspectionError(
                "synthetic_source_mismatch",
                "synthetic_demo bundles must declare source=synthetic_demo",
            )
        )


def inspect_bundle(bundle_path: Path) -> InspectionResult:
    errors: list[InspectionError] = []
    manifest_file = bundle_path / "manifest.json"

    if not manifest_file.exists():
        return InspectionResult(
            valid=False,
            errors=[InspectionError("missing_manifest", "manifest.json not found")],
        )

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        message = f"manifest.json is not valid JSON: {error}"
        return InspectionResult(
            valid=False,
            errors=[InspectionError("invalid_manifest", message)],
        )

    _validate_source_policy(manifest, errors)

    declared: dict[str, str] = manifest.get("files", {}) or {}
    actual_hashes: dict[str, str] = {}
    payload_files = sorted(
        p for p in bundle_path.rglob("*") if p.is_file() and p.name != "manifest.json"
    )

    # Verify declared files.
    for name, expected_hash in declared.items():
        target = _safe_relative(bundle_path, name)
        if target is None:
            errors.append(InspectionError("invalid_path", f"declared file escapes bundle: {name}"))
            continue
        if not target.exists():
            errors.append(InspectionError("missing_file", f"declared file missing: {name}", name))
            continue
        actual = _sha256(target)
        actual_hashes[name] = actual
        if actual != expected_hash:
            errors.append(
                InspectionError("snapshot_integrity_error", f"hash mismatch for {name}", name)
            )

    # Detect undeclared payload files.
    declared_resolved = set()
    for name in declared:
        target = _safe_relative(bundle_path, name)
        if target is not None:
            declared_resolved.add(target)
    for payload in payload_files:
        if payload in declared_resolved:
            continue
        name = payload.name
        errors.append(InspectionError("unexpected_file", f"undeclared payload file: {name}", name))

    valid = not errors
    return InspectionResult(
        valid=valid,
        bundle_id=manifest.get("bundle_id"),
        errors=errors,
        actual_hashes=actual_hashes,
    )
