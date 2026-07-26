import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bidscope.domain.snapshots import SnapshotManifest
from pydantic import ValidationError


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
    #: The parsed manifest, available when inspection succeeded. Adapters read
    #: this instead of re-parsing ``manifest.json`` a second time.
    manifest: SnapshotManifest | None = None


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a regular file.

    Raises :class:`OSError` (e.g. permission denied) so the caller can convert
    it into a typed :class:`InspectionError` instead of leaking a raw system
    exception.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(base: Path, candidate: str) -> Path | None:
    """Return the resolved path for a declared file, or None if it escapes base.

    Rejects path traversal that would land outside the bundle directory. Symlink
    rejection happens before this helper resolves the path so callers can report
    symlinks as an invalid file type rather than treating their targets as files.
    """
    if ".." in candidate.split("/") or candidate.startswith("/"):
        return None
    resolved = (base / candidate).resolve()
    if base.resolve() != resolved and base.resolve() not in resolved.parents:
        return None
    return resolved


def _contains_symlink(base: Path, candidate: str) -> bool:
    """Return whether a declared path or any parent component is a symlink."""
    raw_path = base / candidate
    try:
        relative_parts = raw_path.relative_to(base).parts
    except ValueError:
        return False

    current = base
    for part in relative_parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _convert_manifest_errors(data: dict[str, Any]) -> list[InspectionError]:
    """Validate the manifest through the typed SnapshotManifest contract.

    Any schema, enum, host-policy or cross-field (source/capture/host) violation
    is converted into a structured :class:`InspectionError` so callers never see
    a raw ``ValidationError`` or ``TypeError``.
    """
    try:
        SnapshotManifest.model_validate(data)
        return []
    except ValidationError as error:
        results: list[InspectionError] = []
        for err in error.errors():
            loc = ".".join(str(part) for part in err["loc"])
            err_type = err["type"]
            msg = err["msg"]
            if err_type == "value_error":
                if "source=synthetic_demo" in msg or "synthetic_demo bundles" in msg:
                    code = "source_capture_mismatch"
                elif "HTTPS" in msg or "example.invalid" in msg:
                    code = "invalid_source_url"
                elif "timezone-aware" in msg:
                    code = "invalid_timestamp"
                elif "SHA-256" in msg:
                    code = "invalid_file_hash"
                elif "official" in msg.lower():
                    code = "invalid_source_url"
                else:
                    code = "invalid_manifest_field"
            elif err_type == "missing":
                code = "missing_manifest_field"
            elif "enum" in err_type:
                code = "invalid_enum_value"
            else:
                code = "invalid_manifest_field"
            results.append(InspectionError(code=code, message=f"{loc}: {msg}", path=loc or None))
        return results


def inspect_bundle(bundle_path: Path) -> InspectionResult:
    # Resolve to an absolute path so rglob results and declared-path resolution
    # share a common root; otherwise relative inputs are wrongly flagged invalid.
    bundle_path = bundle_path.resolve()
    manifest_file = bundle_path / "manifest.json"

    if not manifest_file.exists():
        return InspectionResult(
            valid=False,
            errors=[InspectionError("missing_manifest", "manifest.json not found")],
        )

    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return InspectionResult(
            valid=False,
            errors=[
                InspectionError("invalid_manifest", f"manifest.json is not valid JSON: {error}")
            ],
        )

    if not isinstance(raw, dict):
        return InspectionResult(
            valid=False,
            errors=[InspectionError("invalid_manifest", "manifest.json must be a JSON object")],
        )

    # Single entry point: every structural constraint is validated by the typed
    # SnapshotManifest contract. Failures become typed InspectionErrors.
    errors = _convert_manifest_errors(raw)
    if errors:
        return InspectionResult(valid=False, errors=errors)

    manifest = SnapshotManifest.model_validate(raw)

    # File-integrity checks (hashes, missing and undeclared payload files).
    declared = manifest.files or {}
    actual_hashes: dict[str, str] = {}
    payload_files = sorted(
        p for p in bundle_path.rglob("*") if p.is_file() and p.name != "manifest.json"
    )

    for name, expected_hash in declared.items():
        if _contains_symlink(bundle_path, name):
            errors.append(
                InspectionError(
                    "invalid_file_type",
                    f"declared file must be a regular file, got: {name}",
                    name,
                )
            )
            continue
        target = _safe_relative(bundle_path, name)
        if target is None:
            errors.append(
                InspectionError("invalid_path", f"declared file escapes bundle: {name}", name)
            )
            continue
        if not target.exists():
            errors.append(InspectionError("missing_file", f"declared file missing: {name}", name))
            continue
        # Reject anything that is not a regular file: directories, FIFOs,
        # devices, etc.
        if not target.is_file():
            errors.append(
                InspectionError(
                    "invalid_file_type",
                    f"declared file must be a regular file, got: {name}",
                    name,
                )
            )
            continue
        try:
            actual = _sha256(target)
        except OSError as error:
            # Convert raw OS errors (permission denied, I/O errors) into typed
            # inspection errors so malicious manifests cannot crash inspection.
            errors.append(
                InspectionError(
                    "file_read_error",
                    f"could not read declared file {name}: {error}",
                    name,
                )
            )
            continue
        actual_hashes[name] = actual
        if actual.lower() != expected_hash.lower():
            errors.append(
                InspectionError("snapshot_integrity_error", f"hash mismatch for {name}", name)
            )

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
        bundle_id=manifest.bundle_id,
        errors=errors,
        actual_hashes=actual_hashes,
        manifest=manifest if valid else None,
    )
