"""Graph assembly for the BidScope query workflow.

:func:`build_graph` compiles the first six nodes into aLangGraph
:class:`~langgraph.graph.state.StateGraph`, wiring edges and injecting the
runtime dependencies (ports, searcher, clock, view loader) through
``configurable["deps"]`` so the nodes stay free of global state.

The graph is compiled with a caller-supplied ``checkpointer`` —
:class:`~langgraph.checkpoint.memory.InMemorySaver` in tests and public-demo
runs, :class:`~langgraph.checkpoint.postgres.aio.AsyncPostgresSaver` in durable
deployments — and a ``recursion_limit`` of 16.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph

from bidscope.domain.enums import RunStatus
from bidscope.graph import nodes
from bidscope.graph.state import RunState
from bidscope.retrieval.search import HybridSearcher


class Clock(Protocol):
    """The slice of the clock the nodes use to timestamp events."""

    def now(self) -> Any: ...


class ModelLoader(Protocol):
    """Loads :class:`~bidscope.retrieval.deduplication.NoticeView` by version id."""

    def load_notice_views(
        self, notice_version_ids: list[str]
    ) -> dict[str, Any]: ...


class _ViewLoaderProtocol(Protocol):
    def __call__(self, notice_version_ids: list[str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GraphDeps:
    """Runtime dependencies every node needs, carried in the graph config.

    Wrapping the ports and collaborators in one object keeps ``build_graph``'s
    signature stable as the graph grows — nodes reach them via
    ``config["configurable"]["deps"]`` rather than per-node arguments.
    """

    intent_model: Any
    duplicate_model: Any
    report_model: Any
    searcher: HybridSearcher
    clock: Any
    load_notice_views: Callable[[list[str]], dict[str, Any]]
    report_persistence: Any
    #: Semantic Citation Contract verifier. ``None`` (the default) skips
    #: semantic verification so unit tests and minimal wiring stay valid.
    semantic_verifier: Any = None


def _route_after_validate(state: Any) -> str:
    """Route a failed validation to the graph's terminal point."""
    if getattr(state, "status", None) == RunStatus.FAILED:
        return "failed"
    return "proceed"


class QueryWorkflow:
    """A compiled query workflow with its dependencies bound in.

    Wraps a compiled :class:`~langgraph.graph.state.StateGraph` so every
    invoke automatically injects the runtime ``deps`` into
    ``configurable["deps"]`` — nodes reach their collaborators without global
    state, and callers still pass their own ``thread_id``/resume command.
    """

    def __init__(self, deps: GraphDeps, compiled: Any, recursion_limit: int) -> None:
        self._deps = deps
        self._compiled = compiled
        self._recursion_limit = recursion_limit

    def _merge_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        base = dict(config) if config else {}
        configurable = dict(base.get("configurable") or {})
        configurable["deps"] = self._deps
        base["configurable"] = configurable
        base.setdefault("recursion_limit", self._recursion_limit)
        return base

    async def ainvoke(self, input: Any, config: dict[str, Any] | None = None) -> Any:
        return await self._compiled.ainvoke(input, self._merge_config(config))

    async def astream(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        # ``astream`` is an async generator on the compiled graph; preserve that
        # protocol while still injecting ``deps`` into the config.
        async for chunk in self._compiled.astream(input, self._merge_config(config), **kwargs):
            yield chunk

    async def aget_state(self, config: dict[str, Any] | None = None) -> Any:
        return await self._compiled.aget_state(self._merge_config(config))

    def __getattr__(self, name: str) -> Any:
        # Delegate anything else (e.g. aget_state_tuple) to the compiled graph so
        # the wrapper stays transparent while still injecting ``deps``.
        return getattr(self._compiled, name)


def build_graph(
    deps: GraphDeps,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    recursion_limit: int = 16,
) -> QueryWorkflow:
    """Compile the query workflow.

    Starts at ``parse_intent`` and runs through evidence verification,
    synthesis, deterministic report validation, semantic verification
    (:func:`bidscope.graph.nodes.verify_semantics`) and delivery.
    """
    graph = StateGraph(RunState)

    graph.add_node("parse_intent", nodes.parse_intent)
    graph.add_node("validate_intent", nodes.validate_intent)
    graph.add_node("confirm_intent", nodes.confirm_intent)
    graph.add_node("pause", nodes.pause)
    graph.add_node("build_retrieval_plan", nodes.build_retrieval_plan)
    graph.add_node("retrieve_candidates", nodes.retrieve_candidates)
    graph.add_node("resolve_duplicates", nodes.resolve_duplicates)
    graph.add_node("verify_evidence", nodes.verify_evidence)
    graph.add_node("synthesize_report", nodes.synthesize_report)
    graph.add_node("validate_report", nodes.validate_report)
    graph.add_node("verify_semantics", nodes.verify_semantics)
    graph.add_node("persist_and_deliver", nodes.persist_and_deliver)

    graph.set_entry_point("parse_intent")
    graph.add_edge("parse_intent", "validate_intent")
    # A failed validation short-circuits to the finish point; otherwise the run
    # proceeds to confirmation.
    graph.add_conditional_edges(
        "validate_intent",
        _route_after_validate,
        {"failed": "__end__", "proceed": "confirm_intent"},
    )
    graph.add_conditional_edges(
        "confirm_intent",
        nodes.route_after_confirm,
        {"pause": "pause", "proceed": "build_retrieval_plan"},
    )
    graph.add_edge("pause", "build_retrieval_plan")
    graph.add_edge("build_retrieval_plan", "retrieve_candidates")
    graph.add_edge("retrieve_candidates", "resolve_duplicates")
    graph.add_edge("resolve_duplicates", "verify_evidence")
    graph.add_edge("verify_evidence", "synthesize_report")
    graph.add_edge("synthesize_report", "validate_report")
    # A validation failure loops back to synthesis once (retry); otherwise the
    # run proceeds to semantic verification, then delivers or fails as
    # ``EvidenceInsufficient``.
    graph.add_conditional_edges(
        "validate_report",
        nodes.route_after_validate_report,
        {
            "synthesize_report": "synthesize_report",
            "persist_and_deliver": "verify_semantics",
            "__end__": "__end__",
        },
    )
    graph.add_edge("verify_semantics", "persist_and_deliver")

    # Interrupt AFTER ``pause`` so an awaiting-confirmation run pauses with the
    # status already applied; ``Command(resume=...)`` continues retrieval.
    compiled = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=None,
        interrupt_after=["pause"],
        debug=False,
        name="bidscope_query_workflow",
    )
    return QueryWorkflow(deps, compiled, recursion_limit)


__all__ = ["GraphDeps", "QueryWorkflow", "build_graph"]
