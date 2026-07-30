import re
from collections import deque
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.provenance import (
    validate_provenance,
)
from bidscope.domain.types import AwareDatetime

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTRACT_VERSION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+$")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The sole pre-v2 real bundle admitted before authorization contracts existed.
# This immutable migration register is intentionally exact-match only: dates,
# names, or other bundle attributes must never imply legacy admission.
_LEGACY_CCGP_CURATED_BUNDLE_IDS = frozenset({"ccgp-central-20260718"})


BoundedContractLabel = Annotated[str, Field(min_length=1, max_length=128)]


class AuthorizedSourceContract(BaseModel):
    """Bounded, non-secret admission metadata for an authorized source batch."""

    contract_version: Annotated[str, Field(min_length=1, max_length=64)]
    authorization_ref: BoundedContractLabel
    data_owner: BoundedContractLabel
    regions: Annotated[list[BoundedContractLabel], Field(min_length=1, max_length=50)]
    categories: Annotated[list[BoundedContractLabel], Field(min_length=1, max_length=50)]
    review_status: Literal["approved", "pending", "rejected"]
    reviewed_at: AwareDatetime | None = None
    update_sla: Literal["weekly"]
    retention_days: Annotated[int, Field(gt=0, le=3650)]

    @field_validator("contract_version")
    @classmethod
    def _validate_contract_version(cls, value: str) -> str:
        if not _CONTRACT_VERSION_RE.fullmatch(value):
            raise ValueError("contract_version must use a name-vN format")
        return value

    @field_validator("authorization_ref", "data_owner", "regions", "categories")
    @classmethod
    def _reject_control_characters(cls, value: object) -> object:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and (
                not item.strip()
                or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in item)
            ):
                raise ValueError(
                    "contract labels must be non-blank and contain no control characters"
                )
        return value

    @model_validator(mode="after")
    def _validate_review(self) -> "AuthorizedSourceContract":
        if self.review_status == "approved" and self.reviewed_at is None:
            raise ValueError("approved data_contract requires reviewed_at")
        return self


class SnapshotManifest(BaseModel):
    """Validated snapshot bundle provenance manifest.

    This model is the single source of truth for manifest structure. The bundle
    adapter (:func:`bidscope.snapshots.adapters.inspect_bundle`) routes every
    manifest through :meth:`model_validate` so that schema, enum, host-policy
    and cross-field (source/capture/host) violations all surface as structured
    errors rather than raw ``TypeError``\\ s.
    """

    schema_version: Annotated[int, Field(ge=1)] = 1
    bundle_id: Annotated[str, Field(min_length=1)]
    source: SourceName
    capture_kind: CaptureKind
    source_urls: Annotated[list[HttpUrl], Field(min_length=1)]
    retrieved_at: AwareDatetime
    retrieval_outcome: Annotated[str, Field(min_length=1)]
    parser_version: Annotated[str, Field(min_length=1)]
    files: Annotated[dict[str, str], Field(min_length=1)]
    batch_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    data_contract: AuthorizedSourceContract | None = None

    @field_validator("source_urls", mode="before")
    @classmethod
    def _validate_raw_source_urls(cls, value: object) -> list[str]:
        """Accept only JSON-compatible raw URL collections before URL parsing."""
        if type(value) not in (list, tuple, set, frozenset, deque):
            raise ValueError("source_urls must be a supported raw string collection")
        source_urls = cast(
            list[object] | tuple[object, ...] | set[object] | frozenset[object] | deque[object],
            value,
        )
        raw_source_urls: list[str] = []
        for raw_url in source_urls:
            if type(raw_url) is not str:
                raise ValueError("source_urls entries must be raw strings")
            if "@" in urlsplit(raw_url).netloc:
                raise ValueError("source_urls must not include URL credentials")
            raw_source_urls.append(raw_url)
        return raw_source_urls

    @field_validator("source_urls")
    @classmethod
    def _require_https(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        for url in value:
            if url.scheme != "https":
                raise ValueError("source_urls must use HTTPS scheme")
            if url.username is not None or url.password is not None:
                raise ValueError("source_urls must not include URL credentials")
            if url.port not in (None, 443):
                raise ValueError("source_urls must use the default HTTPS port")
        return value

    @field_validator("files")
    @classmethod
    def _validate_file_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        for name, hash_value in value.items():
            if not _SHA256_RE.match(hash_value):
                raise ValueError(
                    f"file {name} hash must be a 64-character hex SHA-256, got: {hash_value!r}"
                )
        return value

    @field_validator("batch_id")
    @classmethod
    def _validate_batch_id(cls, value: str | None) -> str | None:
        if value is not None and not _BATCH_ID_RE.fullmatch(value):
            raise ValueError("batch_id must be a bounded identifier without path separators")
        return value

    @model_validator(mode="after")
    def _validate_provenance(self) -> "SnapshotManifest":
        # Source/capture_kind agreement is enforced uniformly via the shared
        # provenance validator; host agreement is checked per-URL.
        for url in self.source_urls:
            validate_provenance(
                source=self.source,
                capture_kind=self.capture_kind,
                host=url.host,
                external_id=self.bundle_id,
            ).raise_invalid()
        return self

    @model_validator(mode="after")
    def _validate_authorized_contract(self) -> "SnapshotManifest":
        if self.schema_version == 1:
            if (
                self.source == SourceName.CCGP
                and self.capture_kind == CaptureKind.CURATED_PUBLIC_EXCERPT
                and self.bundle_id not in _LEGACY_CCGP_CURATED_BUNDLE_IDS
            ):
                raise ValueError(
                    "schema_version 1 CCGP curated bundle is not in the legacy admission register"
                )
            return self
        if self.schema_version != 2:
            raise ValueError(f"unsupported snapshot schema_version: {self.schema_version}")
        if (
            self.source != SourceName.CCGP
            or self.capture_kind != CaptureKind.CURATED_PUBLIC_EXCERPT
        ):
            raise ValueError(
                "schema_version 2 is reserved for CCGP curated_public_excerpt bundles"
            )
        if self.batch_id is None:
            raise ValueError("schema_version 2 requires batch_id")
        if self.data_contract is None:
            raise ValueError("schema_version 2 requires a data_contract")
        if self.data_contract.review_status != "approved":
            raise ValueError("authorized data contract review_status must be approved")
        return self
