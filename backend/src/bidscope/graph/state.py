"""Versioned graph state for a BidScope query run.

:class:`RunState` is the single source of truth the ten graph nodes read and
write. It is a Pydantic model (design §5.1) that stores *identifiers* and
bounded references — never raw notice bodies — so a run can be checkpointed,
inspected and resumed without ever copying untrusted source text into state.

List fields use an ``operator.add`` reducer so each node can emit a partial
update (e.g. one node event, one batch of candidate IDs) and LangGraph merges
it into the accumulated value. The schema is frozen at task 10: later tasks
populate the fields they own (``verified_opportunities``, ``report``, …) but do
not add new ones, so this file is not modified after this task.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from bidscope.domain.enums import RunStatus
from bidscope.domain.intents import SearchIntent
from bidscope.domain.reports import Report
from bidscope.domain.runs import RunEvent, SerializableError
from bidscope.llm.types import ModelUsage, VerifiedOpportunity


@dataclass(frozen=True)
class RetrievalPlan:
    """The resolved retrieval intent produced by ``build_retrieval_plan``.

    Carries the query terms the searcher should embed/lexically match plus the
    structured filters (region, date window, budget floor) derived from the
    confirmed :class:`~bidscope.domain.intents.SearchIntent`. A frozen
    dataclass so it serialises cleanly through the checkpoint serde.
    """

    query_terms: tuple[str, ...]
    regions: tuple[str, ...]
    published_from: datetime | None
    published_to: datetime | None
    min_budget_minor_units: int | None


@dataclass(frozen=True)
class DuplicateGroup:
    """A cluster of notices that resolve to the same opportunity.

    ``representative_id`` is the notice version that survives; ``member_ids``
    are the versions it subsumes. ``decision`` records the deduplication
    verdict (``exact`` / ``ambiguous`` / ``distinct``) for auditability.
    """

    representative_id: str
    member_ids: tuple[str, ...]
    decision: str


class RunState(BaseModel):
    """All data a query run carries between nodes.

    Field order follows design §5.1. Accumulating list fields are annotated
    with an ``operator.add`` reducer; scalar fields are overwritten by the
    node that owns them.
    """

    model_config = {"arbitrary_types_allowed": True}

    run_id: str = ""
    user_request: str = ""
    status: str = RunStatus.PENDING
    search_intent: SearchIntent | None = None
    retrieval_plan: RetrievalPlan | None = None
    candidate_notice_ids: Annotated[list[str], operator.add] = Field(default_factory=list)
    duplicate_groups: Annotated[list[DuplicateGroup], operator.add] = Field(
        default_factory=list
    )
    verified_opportunities: Annotated[list[VerifiedOpportunity], operator.add] = Field(
        default_factory=list
    )
    report: Report | None = None
    node_events: Annotated[list[RunEvent], operator.add] = Field(default_factory=list)
    token_usage: Annotated[list[ModelUsage], operator.add] = Field(default_factory=list)
    latency: dict[str, float] = Field(default_factory=dict)
    errors: Annotated[list[SerializableError], operator.add] = Field(default_factory=list)
    retry_count: int = 0
    degraded_modes: Annotated[list[str], operator.add] = Field(default_factory=list)


__all__ = ["DuplicateGroup", "RetrievalPlan", "RunState"]
