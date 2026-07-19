"""Async protocols for the three model capabilities BidScope relies on.

The graph (:mod:`bidscope.graph`) depends only on these protocols, never on a
particular provider. That keeps the public demo and the test suite on the
deterministic :mod:`bidscope.llm.fake` implementation while a configured
deployment can swap in :mod:`bidscope.llm.deepseek` without touching graph
code.

Each protocol method is ``async`` because real providers issue network calls;
the fake implementation still honors the signature so the two are drop-in
interchangeable.
"""

from __future__ import annotations

from typing import Protocol

from bidscope.clock import Clock
from bidscope.domain.intents import SearchIntent
from bidscope.llm.types import DuplicatePair, ReportDraft, VerifiedOpportunity
from bidscope.retrieval.deduplication import DuplicateClassification


class IntentModel(Protocol):
    """Turn a natural-language request into a structured :class:`SearchIntent`.

    The implementation must derive every date, amount and region from the
    request text plus the injected :class:`~bidscope.clock.Clock`; it must not
    read the system clock directly.
    """

    async def parse(self, request: str, clock: Clock) -> SearchIntent: ...


class DuplicateModel(Protocol):
    """Decide whether two notices are duplicates, distinct, or ambiguous.

    Only ``ambiguous`` pairs reach this port — exact and distinct pairs are
    resolved deterministically by
    :func:`bidscope.retrieval.deduplication.classify_duplicate` before the
    model is consulted.
    """

    async def classify(self, pair: DuplicatePair) -> DuplicateClassification: ...


class ReportModel(Protocol):
    """Synthesize a :class:`ReportDraft` from verified opportunities.

    The model may only summarize fields present on the
    :class:`VerifiedOpportunity` and may only quote from its ``evidence``
    spans. Anything else must stay unknown.
    """

    async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft: ...


__all__ = [
    "IntentModel",
    "DuplicateModel",
    "ReportModel",
    "DuplicatePair",
    "ReportDraft",
    "VerifiedOpportunity",
    "SearchIntent",
    "DuplicateClassification",
]
