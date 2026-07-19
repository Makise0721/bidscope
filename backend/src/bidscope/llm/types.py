"""Value types that cross the LLM port boundary.

The port protocols (:mod:`bidscope.llm.ports`) speak in domain objects
(:class:`~bidscope.domain.intents.SearchIntent`,
:class:`~bidscope.retrieval.deduplication.DuplicateClassification`) but need a
few carrier types of their own for inputs too structured to pass as plain
arguments and for results that pair a draft with its model-usage receipt.
These carriers live here so both the fake and the DeepSeek implementations —
and their tests — share one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field

from bidscope.domain.reports import ReportItem


@dataclass(frozen=True)
class DuplicatePair:
    """The two notices a duplicate classifier must compare.

    ``candidate`` is the newly retrieved notice; ``existing`` is the notice
    already stored. The ordering mirrors
    :func:`bidscope.retrieval.deduplication.classify_duplicate` so a call can
    be forwarded verbatim.
    """

    candidate: object
    existing: object


@dataclass(frozen=True)
class EvidenceSpan:
    """An immutable slice of source text backing a report claim.

    ``notice_id`` pins the span to a single notice so a later evidence check
    can confirm the span has not drifted from the version the report cites.
    """

    evidence_id: str
    text: str
    notice_id: str


@dataclass(frozen=True)
class VerifiedOpportunity:
    """A de-duplicated, evidence-bound opportunity handed to the report model.

    It carries only the identifiers and verified fields the model is allowed to
    summarize — never a raw notice body. The report model may quote the
    ``evidence`` spans verbatim; everything else must stay unknown.
    """

    notice_id: str
    title: str
    region: str | None = None
    purchaser: str | None = None
    budget_raw: str | None = None
    deadline: str | None = None
    summary: str | None = None
    evidence: tuple[EvidenceSpan, ...] = ()


class ReportDraft(BaseModel):
    """The report model's output before persistence-time validation.

    Shares the item shape of :class:`~bidscope.domain.reports.Report` but lives
    outside the frozen domain layer so the model can emit partial or unverified
    drafts. ``validate_report`` is what promotes a draft into a trusted
    :class:`~bidscope.domain.reports.Report`. Implemented as a Pydantic model so
    both the DeepSeek adapter (structured output) and the fake can validate an
    untrusted payload through :meth:`model_validate`.
    """

    model_config = {"frozen": True}

    items: Annotated[list[ReportItem], Field(default_factory=list)]
    freshness_window: str | None = None
    source_availability: Annotated[list[str], Field(default_factory=list)]
    completeness_warning: str | None = None
    assumptions: Annotated[list[str], Field(default_factory=list)]


@dataclass(frozen=True)
class ModelUsage:
    """A receipt for a single model invocation.

    Recorded after every call so the graph's ``token_usage`` accumulator
    contributes real latency and token figures rather than estimates. The
    ``pricing_snapshot`` field pins the price-per-token that applied, so cost
    can be reconstructed later even if prices change.
    """

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    pricing_snapshot: str = "unspecified"


__all__ = [
    "DuplicatePair",
    "EvidenceSpan",
    "VerifiedOpportunity",
    "ReportDraft",
    "ModelUsage",
]
