from typing import Annotated, Any

from pydantic import BaseModel, Field

from bidscope.domain.types import AwareDatetime, BidScopeErrorCode


class RunEvent(BaseModel):
    node: str
    event: str
    status: str
    timestamp: AwareDatetime
    message: str | None = None
    details: Annotated[dict[str, Any], Field(default_factory=dict)]


class SerializableError(BaseModel):
    code: BidScopeErrorCode
    message: str
    details: Annotated[dict[str, Any], Field(default_factory=dict)]
