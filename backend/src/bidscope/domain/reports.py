from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from bidscope.domain.types import AwareDatetime


class ReportCitation(BaseModel):
    evidence_id: str
    label: str | None = None


class ReportClaim(BaseModel):
    text: str
    citation_ids: list[str]

    @model_validator(mode="after")
    def _require_citations(self) -> "ReportClaim":
        if not self.citation_ids:
            raise ValueError("ReportClaim must reference at least one citation")
        return self


class ReportItem(BaseModel):
    notice_id: str
    title: str
    known_fields: Annotated[dict[str, str], Field(default_factory=dict)]
    unknown_fields: Annotated[list[str], Field(default_factory=list)]
    relevance_reason: str | None = None
    risk_note: str | None = None
    citations: Annotated[list[ReportCitation], Field(default_factory=list)]
    claims: Annotated[list[ReportClaim], Field(default_factory=list)]


class Report(BaseModel):
    run_id: str
    generated_at: AwareDatetime
    query_conditions: dict[str, str]
    freshness_window: str | None = None
    source_availability: Annotated[list[str], Field(default_factory=list)]
    completeness_warning: str | None = None
    items: Annotated[list[ReportItem], Field(default_factory=list)]
