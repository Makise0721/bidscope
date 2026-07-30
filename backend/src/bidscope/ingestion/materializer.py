"""Deterministic materialization of authorized source responses."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.provenance import OFFICIAL_HOSTS_BY_SOURCE
from bidscope.domain.snapshots import (
    AuthorizedAcquisitionMetadata,
    AuthorizedSourceContract,
    SnapshotManifest,
)
from bidscope.ingestion.ports import AuthorizedSourcePage
from bidscope.snapshots.adapters import InspectionResult, inspect_bundle

_SENSITIVE_METADATA_PARTS = (
    "secret",
    "password",
    "credential",
    "signing",
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "auth",
    "token",
)


class BundleQuarantineError(RuntimeError):
    """Raised when a response cannot be admitted as an immutable bundle."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MaterializedBundle:
    path: Path
    manifest: SnapshotManifest
    bundle_id: str
    response_sha256: str


class AuthorizedBundleMaterializer:
    """Write a content-addressed raw-response bundle and inspect it."""

    def __init__(self, staging_root: Path, *, max_bundle_bytes: int = 200 * 1024 * 1024) -> None:
        if max_bundle_bytes <= 0:
            raise ValueError("max_bundle_bytes must be positive")
        self.staging_root = staging_root
        self.max_bundle_bytes = max_bundle_bytes

    def materialize(
        self,
        page: AuthorizedSourcePage,
        *,
        batch_id: str,
        data_contract: AuthorizedSourceContract | None,
        retrieval_outcome: str = "authorized_source_success",
        parser_version: str = "ccgp-authorized-v1",
        extra_metadata: Mapping[str, object] | None = None,
    ) -> MaterializedBundle:
        if data_contract is None:
            raise BundleQuarantineError(
                "missing_data_contract", "authorized data contract is missing"
            )
        if data_contract.review_status != "approved":
            raise BundleQuarantineError(
                "authorization_not_approved", "authorized data contract is not approved"
            )
        self._validate_source_url(page.source_url)
        actual_hash = sha256(page.response_bytes).hexdigest()
        if not page.response_sha256:
            raise BundleQuarantineError("missing_response_hash", "source response hash is missing")
        if page.response_sha256.lower() != actual_hash:
            raise BundleQuarantineError(
                "response_hash_mismatch", "source response hash mismatched"
            )
        if len(page.response_bytes) > self.max_bundle_bytes:
            raise BundleQuarantineError(
                "response_too_large", "source response exceeds bundle limit"
            )
        safe_extra = self._safe_extra_metadata(extra_metadata or {})
        try:
            acquisition_metadata = AuthorizedAcquisitionMetadata(
                response_sha256=actual_hash,
                status_code=page.status_code,
                cursor_before=page.cursor_before,
                cursor_after=page.next_cursor,
                record_count=len(page.items),
                response_items_field=page.response_items_field,
                notice_field_map=dict(page.notice_field_map),
                extra=safe_extra,
            )
        except ValidationError as error:
            raise BundleQuarantineError(
                "invalid_metadata", "authorized acquisition metadata is invalid"
            ) from error

        identity = {
            "acquisition_metadata": acquisition_metadata.model_dump(mode="json"),
            "batch_id": batch_id,
            "data_contract": data_contract.model_dump(mode="json"),
            "cursor_before": page.cursor_before,
            "next_cursor": page.next_cursor,
            "parser_version": parser_version,
            "retrieval_outcome": retrieval_outcome,
            "retrieved_at": page.retrieved_at.isoformat(),
            "response_sha256": actual_hash,
            "source_url": page.source_url,
        }
        identity_bytes = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        bundle_id = f"ccgp-live-{sha256(page.response_bytes + b"\n" + identity_bytes).hexdigest()}"
        manifest = self._build_manifest(
            page=page,
            batch_id=batch_id,
            bundle_id=bundle_id,
            data_contract=data_contract,
            acquisition_metadata=acquisition_metadata,
            parser_version=parser_version,
            retrieval_outcome=retrieval_outcome,
        )
        manifest_bytes = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target = self.staging_root / bundle_id
        self.staging_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return self._reuse_existing(target, manifest, manifest_bytes, page.response_bytes)

        temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_id}.", dir=self.staging_root))
        committed = False
        try:
            (temporary / "response.json").write_bytes(page.response_bytes)
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            total_bytes = sum(
                path.stat().st_size for path in temporary.rglob("*") if path.is_file()
            )
            if total_bytes > self.max_bundle_bytes:
                raise BundleQuarantineError(
                    "bundle_too_large", "materialized source bundle exceeds the configured limit"
                )
            inspection = inspect_bundle(temporary)
            if not inspection.valid or inspection.manifest is None:
                self._raise_quarantine(inspection)
            temporary.replace(target)
            committed = True
        except BundleQuarantineError:
            raise
        except ValidationError as error:
            raise BundleQuarantineError(
                "invalid_metadata", "authorized acquisition metadata is invalid"
            ) from error
        except OSError as error:
            raise BundleQuarantineError(
                "materialization_failed", "could not materialize source bundle"
            ) from error
        finally:
            if not committed:
                shutil.rmtree(temporary, ignore_errors=True)
        return MaterializedBundle(target, manifest, bundle_id, actual_hash)

    @staticmethod
    def _build_manifest(
        *,
        page: AuthorizedSourcePage,
        batch_id: str,
        bundle_id: str,
        data_contract: AuthorizedSourceContract,
        acquisition_metadata: AuthorizedAcquisitionMetadata,
        parser_version: str,
        retrieval_outcome: str,
    ) -> SnapshotManifest:
        try:
            return SnapshotManifest.model_validate(
                {
                    "schema_version": 2,
                    "bundle_id": bundle_id,
                    "source": SourceName.CCGP,
                    "capture_kind": CaptureKind.RAW_RESPONSE,
                    "source_urls": [page.source_url],
                    "retrieved_at": page.retrieved_at,
                    "retrieval_outcome": retrieval_outcome,
                    "parser_version": parser_version,
                    "files": {"response.json": acquisition_metadata.response_sha256},
                    "batch_id": batch_id,
                    "data_contract": data_contract,
                    "acquisition_metadata": acquisition_metadata,
                }
            )
        except ValidationError as error:
            raise BundleQuarantineError(
                "invalid_manifest", "authorized bundle manifest is invalid"
            ) from error

    @staticmethod
    def _safe_extra_metadata(extra_metadata: Mapping[str, object]) -> dict[str, str]:
        def walk(value: object) -> None:
            if isinstance(value, Mapping):
                for raw_key, item in value.items():
                    if not isinstance(raw_key, str):
                        raise BundleQuarantineError(
                            "invalid_metadata", "acquisition metadata keys must be strings"
                        )
                    normalized = raw_key.casefold().replace("-", "_")
                    if any(part in normalized for part in _SENSITIVE_METADATA_PARTS):
                        raise BundleQuarantineError(
                            "credential_metadata", "credential-bearing metadata is not admitted"
                        )
                    walk(item)
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                lowered = value.casefold()
                if any(
                    marker in lowered
                    for marker in (
                        "bearer ",
                        "basic ",
                        "password=",
                        "secret=",
                        "api-key",
                        "authorization:",
                    )
                ):
                    raise BundleQuarantineError(
                        "credential_metadata", "credential-bearing metadata is not admitted"
                    )
            elif value is not None and not isinstance(value, (bool, int, float)):
                raise BundleQuarantineError(
                    "invalid_metadata", "acquisition metadata values are not supported"
                )

        walk(extra_metadata)
        safe: dict[str, str] = {}
        for key, value in extra_metadata.items():
            if isinstance(value, (str, bool, int, float)):
                safe[key] = str(value)
            else:
                raise BundleQuarantineError(
                    "invalid_metadata", "acquisition metadata values must be scalar"
                )
        return safe

    @staticmethod
    def _validate_source_url(source_url: str) -> None:
        parsed = urlsplit(source_url)
        official_hosts = OFFICIAL_HOSTS_BY_SOURCE[SourceName.CCGP]
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in official_hosts
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BundleQuarantineError(
                "invalid_source_url", "authorized source URL is not approved"
            )

    @staticmethod
    def _raise_quarantine(inspection: InspectionResult) -> None:
        first = inspection.errors[0] if inspection.errors else None
        raise BundleQuarantineError(
            first.code if first is not None else "invalid_bundle",
            "authorized source bundle failed integrity inspection",
        )

    def _reuse_existing(
        self,
        target: Path,
        manifest: SnapshotManifest,
        manifest_bytes: bytes,
        response_bytes: bytes,
    ) -> MaterializedBundle:
        if target.is_symlink() or not target.is_dir():
            raise BundleQuarantineError(
                "bundle_collision", "existing source bundle is not a directory"
            )
        root = target.parent.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise BundleQuarantineError(
                "bundle_collision", "existing source bundle escapes staging"
            )
        total_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
        if total_bytes > self.max_bundle_bytes:
            raise BundleQuarantineError(
                "bundle_too_large", "existing source bundle exceeds the configured limit"
            )
        inspection = inspect_bundle(target)
        if (
            not inspection.valid
            or inspection.manifest is None
            or (target / "manifest.json").read_bytes() != manifest_bytes
            or (target / "response.json").read_bytes() != response_bytes
        ):
            raise BundleQuarantineError(
                "bundle_collision", "existing source bundle is not identical"
            )
        return MaterializedBundle(
            target, manifest, manifest.bundle_id, sha256(response_bytes).hexdigest()
        )
