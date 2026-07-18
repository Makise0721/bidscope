from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from bidscope.domain.enums import CaptureKind, SourceName


class SnapshotManifest(BaseModel):
    schema_version: Annotated[int, Field(ge=1)] = 1
    bundle_id: str
    source: SourceName
    capture_kind: CaptureKind
    source_urls: list[HttpUrl]
    retrieved_at: datetime
    retrieval_outcome: str
    parser_version: str
    files: dict[str, str]

    @field_validator("source_urls")
    @classmethod
    def _require_https(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        for url in value:
            if url.scheme != "https":
                raise ValueError("source_urls must use HTTPS scheme")
        return value

    @field_validator("retrieved_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _synthetic_requires_example_invalid(self) -> "SnapshotManifest":
        if self.capture_kind == CaptureKind.SYNTHETIC_DEMO:
            for url in self.source_urls:
                if url.host != "example.invalid":
                    raise ValueError(
                        "synthetic_demo bundles must use https://example.invalid/ URLs"
                    )
        return self
