"""The first six nodes of the BidScope query workflow.

Each node is a small async function that reads :class:`RunState`, calls exactly
one injected dependency, and returns a partial state update. None of them
touch the database or the network directly — that work lives in the injected
:mod:`bidscope.llm.ports` and the :class:`~bidscope.retrieval.search.HybridSearcher`.

Nodes are registered with the single ``state`` argument LangGraph expects; the
runtime :class:`~langchain_core.runnables.config.RunnableConfig` (carrying the
injected ``deps``) is read inside each node via ``get_config()``.

``confirm_intent`` is the only node that calls :func:`langgraph.types.interrupt`.
It pauses for human confirmation on every scheduled query and on any run with
a low-confidence or conflicting required field; approving resumes the run,
rejecting fails it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from bidscope.domain.enums import RunStatus
from bidscope.domain.runs import RunEvent, SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.graph.state import DuplicateGroup, RetrievalPlan
from bidscope.llm.types import DuplicatePair
from bidscope.retrieval.deduplication import classify_duplicate
from bidscope.retrieval.search import RetrievalFilter


def _event(config: RunnableConfig, state: Any, node: str, event: str, status: str) -> RunEvent:
    """Record a timestamped node event using the injected clock."""
    from datetime import UTC, datetime  # local import keeps module load light

    clock = getattr(_deps(config), "clock", None)
    timestamp = clock.now() if clock else datetime.now(UTC)
    return RunEvent(node=node, event=event, status=status, timestamp=timestamp, details={})


def _deps(config: RunnableConfig) -> Any:
    """Read the injected :class:`GraphDeps` from the runnable config."""
    return config["configurable"]["deps"]


async def parse_intent(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Turn the natural-language request into a structured intent."""
    deps = _deps(config)
    request = state.user_request
    intent = await deps.intent_model.parse(request, deps.clock)
    usage = deps.intent_model.last_usage
    return {
        "search_intent": intent,
        "status": RunStatus.VALIDATE_INTENT,
        "node_events": [_event(config, state, "parse_intent", "intent_parsed", "ok")],
        "token_usage": [usage] if usage else [],
    }


def validate_intent(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Deterministically reject intents whose conditions are self-contradictory."""
    intent = state.search_intent
    errors: list[SerializableError] = []

    dates_inverted = (
        intent.published_from is not None
        and intent.published_to is not None
        and intent.published_from > intent.published_to
    )
    if dates_inverted:
        errors.append(SerializableError(
            code=BidScopeErrorCode.INTENT_INVALID,
            message="published_from must not be after published_to",
            details={"published_from": intent.published_from.isoformat(),
                      "published_to": intent.published_to.isoformat()},
        ))
    budget_inverted = (
        intent.min_budget is not None
        and intent.max_budget is not None
        and intent.min_budget.minor_units > intent.max_budget.minor_units
    )
    if budget_inverted:
        errors.append(SerializableError(
            code=BidScopeErrorCode.INTENT_INVALID,
            message="min_budget must not exceed max_budget",
            details={},
        ))
    if not intent.topics or not intent.regions:
        errors.append(SerializableError(
            code=BidScopeErrorCode.INTENT_INVALID,
            message="intent must specify at least one topic and one region",
            details={},
        ))

    if errors:
        return {
            "status": RunStatus.FAILED,
            "errors": errors,
            "node_events": [_event(config, state, "validate_intent", "intent_invalid", "error")],
        }
    return {
        "status": RunStatus.CONFIRM_INTENT,
        "node_events": [_event(config, state, "validate_intent", "intent_valid", "ok")],
    }


def confirm_intent(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Decide whether the run needs human confirmation.

    A scheduled query or any run whose confidence is below 0.5 routes to the
    ``pause`` node (which the graph compiles with ``interrupt_after``); the
    returned ``awaiting_confirmation`` status is applied *before* the pause, so
    an interrupted run reports the right status. A high-confidence, unscheduled
    query flows straight to retrieval planning without pausing.
    """
    intent = state.search_intent
    if intent.schedule is not None or intent.confidence < 0.5:
        return {
            "status": RunStatus.AWAITING_CONFIRMATION,
            "node_events": [_event(config, state, "confirm_intent", "needs_confirmation", "ok")],
        }
    return {
        "status": RunStatus.BUILD_RETRIEVAL_PLAN,
        "node_events": [_event(config, state, "confirm_intent", "auto_confirmed", "ok")],
    }


def pause(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """A no-op node placed so the graph can interrupt AFTER awaiting status is set.

    LangGraph applies the preceding node's ``awaiting_confirmation`` update and
    then pauses here; ``Command(resume=...)`` continues to retrieval planning.
    """
    return {}


def route_after_confirm(state: Any) -> str:
    """Route to the pause node when confirmation is required, else proceed."""
    if getattr(state, "status", None) == RunStatus.AWAITING_CONFIRMATION:
        return "pause"
    return "proceed"


def build_retrieval_plan(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Derive a :class:`RetrievalPlan` from the confirmed intent."""
    intent = state.search_intent
    terms = list(dict.fromkeys([*intent.topics, *intent.expanded_terms]))
    plan = RetrievalPlan(
        query_terms=tuple(terms),
        regions=tuple(intent.regions),
        published_from=intent.published_from,
        published_to=intent.published_to,
        min_budget_minor_units=intent.min_budget.minor_units if intent.min_budget else None,
    )
    return {
        "retrieval_plan": plan,
        "status": RunStatus.RETRIEVE_CANDIDATES,
        "node_events": [_event(config, state, "build_retrieval_plan", "plan_built", "ok")],
    }


async def retrieve_candidates(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Run hybrid retrieval; degrade lexically when embedding is unavailable."""
    deps = _deps(config)
    plan = state.retrieval_plan
    filters = RetrievalFilter(
        regions=list(plan.regions) if plan.regions else None,
        published_from=plan.published_from,
        published_to=plan.published_to,
        min_budget_minor_units=plan.min_budget_minor_units,
    )
    query = " ".join(plan.query_terms)
    result = await deps.searcher.search(query, filters)
    candidate_ids = [candidate.notice_version_id for candidate in result.candidates]
    return {
        "candidate_notice_ids": candidate_ids,
        "degraded_modes": list(result.degraded_modes),
        "status": RunStatus.RESOLVE_DUPLICATES,
        "node_events": [_event(
            config, state, "retrieve_candidates",
            "lexical_only" if result.degraded_modes else "hybrid",
            "degraded" if result.degraded_modes else "ok",
        )],
    }


async def resolve_duplicates(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Exact/ambiguous dedup: only ambiguous pairs reach the model port."""
    deps = _deps(config)
    views_by_id = dict(deps.load_notice_views(list(state.candidate_notice_ids)))

    groups: list[DuplicateGroup] = []
    candidate_ids = list(state.candidate_notice_ids)
    for source_id in candidate_ids:
        source = views_by_id.get(source_id)
        if source is None:
            continue
        for target_id in candidate_ids:
            if target_id == source_id:
                continue
            target = views_by_id.get(target_id)
            if target is None:
                continue
            decision = classify_duplicate(source, target)
            if decision.decision == "ambiguous":
                model_decision = await deps.duplicate_model.classify(DuplicatePair(source, target))
                groups.append(DuplicateGroup(
                    representative_id=source_id,
                    member_ids=(target_id,),
                    decision=model_decision.decision,
                ))
            elif decision.decision == "exact":
                groups.append(DuplicateGroup(
                    representative_id=source_id,
                    member_ids=(target_id,),
                    decision="exact",
                ))

    return {
        "duplicate_groups": groups,
        "status": RunStatus.CANDIDATES_RESOLVED,
        "node_events": [_event(config, state, "resolve_duplicates", "duplicates_resolved", "ok")],
    }


__all__ = [
    "build_retrieval_plan",
    "confirm_intent",
    "parse_intent",
    "pause",
    "resolve_duplicates",
    "retrieve_candidates",
    "route_after_confirm",
    "validate_intent",
]
