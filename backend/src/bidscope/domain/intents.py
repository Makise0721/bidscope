from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from bidscope.domain.notices import Money


class RunSchedule(BaseModel):
    cron_expression: str
    timezone: str = "Asia/Shanghai"


class SearchIntent(BaseModel):
    topics: list[str]
    expanded_terms: Annotated[list[str], Field(default_factory=list)]
    regions: Annotated[list[str], Field(default_factory=list)]
    published_from: datetime | None = None
    published_to: datetime | None = None
    min_budget: Money | None = None
    max_budget: Money | None = None
    schedule: RunSchedule | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    assumptions: Annotated[list[str], Field(default_factory=list)]

    @model_validator(mode="after")
    def _require_timezone_aware_timestamps(self) -> "SearchIntent":
        if self.published_from is not None and self.published_from.tzinfo is None:
            raise ValueError("published_from must be timezone-aware")
        if self.published_to is not None and self.published_to.tzinfo is None:
            raise ValueError("published_to must be timezone-aware")
        return self

    @model_validator(mode="after")
    def _validate_date_range(self) -> "SearchIntent":
        if (
            self.published_from is not None
            and self.published_to is not None
            and self.published_from > self.published_to
        ):
            raise ValueError("published_from must not be after published_to")
        return self

    @model_validator(mode="after")
    def _validate_budget_range(self) -> "SearchIntent":
        if (
            self.min_budget is not None
            and self.max_budget is not None
            and self.min_budget.minor_units > self.max_budget.minor_units
        ):
            raise ValueError("min_budget must not exceed max_budget")
        return self
