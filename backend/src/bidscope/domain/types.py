from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator


def _require_tz_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


#: Pydantic type that rejects naive datetimes at the boundary.
AwareDatetime = Annotated[datetime, AfterValidator(_require_tz_aware)]


class BidScopeErrorCode(StrEnum):
    """Bounded error codes from the design specification (section 9)."""

    SNAPSHOT_INTEGRITY_ERROR = "snapshot_integrity_error"
    SNAPSHOT_STALE = "snapshot_stale"
    PARSE_DRIFT = "parse_drift"
    INTENT_INVALID = "intent_invalid"
    RETRIEVAL_EMPTY = "retrieval_empty"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    MODEL_TRANSIENT_ERROR = "model_transient_error"
    DELIVERY_ERROR = "delivery_error"
    SNAPSHOT_IMPORT_ERROR = "snapshot_import_error"
    GRAPH_NODE_ERROR = "graph_node_error"
