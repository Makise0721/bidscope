import re
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.provenance import (
    validate_provenance,
)
from bidscope.domain.types import AwareDatetime

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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

    @field_validator("source_urls")
    @classmethod
    def _require_https(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        for url in value:
            if url.scheme != "https":
                raise ValueError("source_urls must use HTTPS scheme")
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
