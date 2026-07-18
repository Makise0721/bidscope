from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    node: str
    event: str
    status: str
    timestamp: datetime
    message: str | None = None
    details: Annotated[dict[str, Any], Field(default_factory=dict)]


class SerializableError(BaseModel):
    code: str
    message: str
    details: Annotated[dict[str, Any], Field(default_factory=dict)]
