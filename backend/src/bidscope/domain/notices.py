from typing import Annotated, Any

from pydantic import BaseModel, Field, HttpUrl, model_validator

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.provenance import validate_provenance
from bidscope.domain.types import AwareDatetime


class Money(BaseModel):
    minor_units: int
    currency: str = "CNY"
    raw_text: str | None = None

    model_config = {"frozen": True}


class NormalizedNotice(BaseModel):
    source: SourceName
    external_id: str
    source_url: HttpUrl
    capture_kind: CaptureKind
    title: str | None = None
    purchaser: str | None = None
    region: str | None = None
    publish_time: AwareDatetime | None = None
    deadline: AwareDatetime | None = None
    budget: Money | None = None
    summary: str | None = None
    parser_version: str
    raw_fields: Annotated[dict[str, Any], Field(default_factory=dict)]

    @model_validator(mode="after")
    def _validate_provenance(self) -> "NormalizedNotice":
        validate_provenance(
            source=self.source,
            capture_kind=self.capture_kind,
            host=self.source_url.host,
            external_id=self.external_id,
        ).raise_invalid()
        return self


class NoticeEvidence(BaseModel):
    notice_version_id: str
    text: str
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]
    span_hash: str
