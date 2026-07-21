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
from bidscope.domain.notices import NoticeEvidence
from bidscope.domain.runs import SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.evidence.extractor import extract_evidence
from bidscope.evidence.validator import validate_report as validate_report_bindings
from bidscope.graph.state import DuplicateGroup, RetrievalPlan
from bidscope.llm.types import DuplicatePair, EvidenceSpan, ReportDraft, VerifiedOpportunity
from bidscope.retrieval.deduplication import classify_duplicate
from bidscope.retrieval.search import RetrievalFilter

#: Maximum number of synthesis retries after a report-validation failure. The
#: first failure retries synthesis once; a second failure gives up.
MAX_SYNTHESIS_RETRIES = 1


def _event(
    config: RunnableConfig, state: Any, node: str, event: str, status: str,
) -> dict[str, Any]:
    """Record a timestamped node event using the injected clock.

    Returns a plain dict (not a model instance) so node events serialise cleanly
    through the LangGraph checkpoint serde and can be re-hydrated in a different
    process.
    """
    from datetime import UTC, datetime  # local import keeps module load light

    clock = getattr(_deps(config), "clock", None)
    timestamp = clock.now() if clock else datetime.now(UTC)
    return {
        "node": node,
        "event": event,
        "status": status,
        "timestamp": timestamp.isoformat(),
        "message": None,
        "details": {},
    }


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
    """Deterministically reject intents whose conditions are self-contradictory.

    Date/budget inversion is also enforced by ``SearchIntent``'s own Pydantic
    validators (and so is caught at parse time); these checks are deliberate
    defence in depth. The independently valuable guard here is the empty
    topics/regions check, which catches semantically empty intents that pass
    Pydantic construction.
    """
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


async def verify_evidence(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Bind each verified opportunity to immutable source evidence spans.

    For every candidate notice, extracts a :class:`~bidscope.domain.notices.NoticeEvidence`
    span per ``claim_supporting_text`` and records both the span (keyed in
    ``evidence_by_id``) and a lightweight :class:`~bidscope.llm.types.EvidenceSpan`
    reference on the :class:`~bidscope.llm.types.VerifiedOpportunity`. The model
    port later quotes only these spans; the validator later checks that every
    reported claim cites one of them.
    """
    deps = _deps(config)
    views_by_id = deps.load_notice_views(list(state.candidate_notice_ids))
    evidence_by_id: dict[str, NoticeEvidence] = {}
    verified: list[VerifiedOpportunity] = []

    for notice_version_id in state.candidate_notice_ids:
        view = views_by_id.get(notice_version_id)
        if view is None:
            continue
        source_text = "\n".join(view.claim_supporting_texts)
        snippets = view.claim_supporting_texts
        spans = extract_evidence(notice_version_id, source_text, snippets)
        evidence_spans = []
        for span in spans:
            evidence_by_id[span.span_hash] = span
            evidence_spans.append(EvidenceSpan(
                evidence_id=span.span_hash,
                text=span.text,
                notice_id=notice_version_id,
            ))
        verified.append(VerifiedOpportunity(
            notice_id=notice_version_id,
            title=view.title or notice_version_id,
            region=view.region,
            purchaser=view.purchaser,
            budget_raw=_format_money(view.budget_minor_units, view.budget_currency),
            evidence=tuple(evidence_spans),
        ))

    return {
        "evidence_by_id": evidence_by_id,
        "verified_opportunities": verified,
        "status": RunStatus.SYNTHESIZE_REPORT,
        "node_events": [_event(config, state, "verify_evidence", "evidence_verified", "ok")],
    }


async def synthesize_report(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Call the report model once per verified opportunity and merge the drafts."""
    deps = _deps(config)
    items = []
    for opportunity in state.verified_opportunities:
        draft = await deps.report_model.synthesize(opportunity)
        items.extend(draft.items)

    report = ReportDraft(items=items) if items else ReportDraft()
    return {
        "report": report,
        "status": RunStatus.VALIDATE_REPORT,
        "node_events": [_event(config, state, "synthesize_report", "report_synthesized", "ok")],
    }


async def validate_report(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Validate the synthesized report against the bound evidence.

    A valid report proceeds to delivery. An invalid one triggers at most one
    synthesis retry (``retry_count``); a second failure records an
    ``EvidenceInsufficient`` error and the run stops — no unsupported report
    is ever delivered.
    """
    result = validate_report_bindings(state.report, state.evidence_by_id)
    if result.valid:
        return {
            "status": RunStatus.PERSIST_AND_DELIVER,
            "node_events": [_event(config, state, "validate_report", "report_valid", "ok")],
        }

    if state.retry_count < MAX_SYNTHESIS_RETRIES:
        return {
            "retry_count": state.retry_count + 1,
            "report": None,
            "status": RunStatus.SYNTHESIZE_REPORT,
            "node_events": [_event(config, state, "validate_report", "report_invalid_retry", "ok")],
        }

    return {
        "status": RunStatus.FAILED,
        "report": None,
        "errors": [SerializableError(
            code=BidScopeErrorCode.EVIDENCE_INSUFFICIENT,
            message=(
                "Report cited unsupported claims after one synthesis retry;"
                " refusing to deliver."
            ),
            details={"validation_errors": list(result.errors)},
        )],
        "node_events": [_event(config, state, "validate_report", "evidence_insufficient", "error")],
    }


async def persist_and_deliver(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Finalize the run: the report is now trusted and the run is complete."""
    return {
        "status": RunStatus.COMPLETED,
        "node_events": [_event(config, state, "persist_and_deliver", "run_completed", "ok")],
    }


def route_after_validate_report(state: Any) -> str:
    """Loop back to synthesis on a retry, otherwise deliver or fail."""
    if state.status == RunStatus.SYNTHESIZE_REPORT:
        return "synthesize_report"
    if state.status == RunStatus.PERSIST_AND_DELIVER:
        return "persist_and_deliver"
    return "__end__"


def _format_money(minor_units: int | None, currency: str | None) -> str | None:
    if minor_units is None:
        return None
    return f"{currency or 'CNY'} {minor_units}"


__all__ = [
    "build_retrieval_plan",
    "confirm_intent",
    "parse_intent",
    "persist_and_deliver",
    "pause",
    "resolve_duplicates",
    "retrieve_candidates",
    "route_after_confirm",
    "route_after_validate_report",
    "synthesize_report",
    "validate_intent",
    "verify_evidence",
]
