"""Application dependencies for the BidScope API.

The API runs a single demo graph (fake deterministic model + hash embeddings)
shared across all requests via ``app.state``. :class:`RunService` wraps that
graph together with the persistence session factory and the object store, and
exposes the run lifecycle operations the routes call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from bidscope.clock import Clock, SystemClock
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore, ObjectStore
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.graph.executor import Command, create_run, execute
from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
from bidscope.persistence.models import NoticeVersion, QueryRun, RunEvent
from bidscope.retrieval.embeddings import HashEmbeddingProvider
from bidscope.retrieval.search import HybridSearcher


def _load_notice_views(
    session_factory: sessionmaker[Session],
    notice_version_ids: list[str],
) -> dict[str, Any]:
    """Build :class:`~bidscope.retrieval.deduplication.NoticeView`s from the DB.

    Called *synchronously* by the graph nodes (no ``await``), so this uses a
    synchronous SQLAlchemy session. Only the fields available on
    :class:`~bidscope.persistence.models.NoticeVersion` are populated; the rest
    stay ``None``. In the local/demo deployment the test database holds no
    notices, so this practically returns an empty mapping.
    """
    from bidscope.retrieval.deduplication import NoticeView

    if not notice_version_ids:
        return {}
    views: dict[str, Any] = {}
    with session_factory() as session:
        result = session.execute(
            sa.select(NoticeVersion).where(NoticeVersion.id.in_(notice_version_ids))
        )
        for version in result.scalars():
            views[version.id] = NoticeView(
                source="synthetic_demo",
                external_id=version.id,
                canonical_url=f"https://example.invalid/{version.id}",
                project_number=None,
                content_hash=version.content_hash,
                title=version.title,
                purchaser=version.purchaser,
                region=version.region,
                budget_minor_units=version.budget_minor_units,
                budget_currency=version.budget_currency,
                deadline=version.deadline,
            )
    return views


def _to_sync_dsn(async_url: str) -> str:
    """Convert an ``asyncpg`` DSN to a synchronous ``psycopg`` (v3) DSN."""
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def build_demo_graph(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    sync_session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
) -> Any:
    """Compile the demo query workflow: fake model + hash embeddings + InMemorySaver."""
    searcher = HybridSearcher(
        session_factory, HashEmbeddingProvider(dimension=1024),
    )
    notice_factory = sync_session_factory or sessionmaker(
        bind=sa.create_engine(_to_sync_dsn(settings.database_url))
    )
    deps = GraphDeps(
        intent_model=FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=searcher,
        clock=clock or SystemClock(),
        load_notice_views=lambda ids: _load_notice_views(notice_factory, ids),
    )
    return build_graph(deps, checkpointer=InMemorySaver())


@dataclass
class RunQueryResult:
    """Serializable shape of a run returned by the API."""

    id: str
    status: str
    user_request: str

    @classmethod
    def from_row(cls, row: QueryRun) -> RunQueryResult:
        return cls(id=row.id, status=row.status, user_request=row.user_request)


class RunService:
    """Run lifecycle operations over the shared demo graph."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        graph: Any,
        object_store: ObjectStore,
        settings: Settings,
        clock: Clock | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.graph = graph
        self.object_store = object_store
        self.settings = settings
        self.clock = clock or SystemClock()
        #: Test-only: name of the node that should fail on the next run.
        #: Set by the ``/api/test-controls/fail-next-node`` route and consumed
        #: once by ``execute_run``.
        self.fail_next_node: str | None = None

    async def create_run(self, user_request: str) -> str:
        """Persist a ``pending`` run and return its id."""
        return await create_run(user_request, session_factory=self.session_factory)

    async def execute_run(self, run_id: str, input: Any) -> dict[str, Any]:  # noqa: ANN401
        """Drive the graph from ``input`` and sync the final status back to the DB.

        Test-only: when ``self.fail_next_node`` is set, the very next run
        short-circuits with a ``retryable`` failure *before* the graph executes,
        then clears the flag so subsequent runs proceed normally.
        """
        fail_node = self.fail_next_node
        if fail_node is not None:
            self.fail_next_node = None
            await self._update_status(run_id, "retryable")
            return {
                "status": "retryable",
                "fail_next_node": fail_node,
                "errors": [
                    {
                        "code": "INJECTED_NODE_FAILURE",
                        "message": (
                            f"Test-only injected failure for node {fail_node!r}"
                        ),
                    }
                ],
            }

        result = await execute(
            self.graph, run_id, input, session_factory=self.session_factory,
        )
        status = result.get("status")
        if status:
            await self._update_status(run_id, status)
        return result

    async def get_run(self, run_id: str) -> QueryRun | None:
        async with self.session_factory() as session:
            return await session.get(QueryRun, run_id)

    async def confirm(self, run_id: str) -> dict[str, Any]:
        """Resume an awaiting-confirmation run. Raises if not confirmable."""
        run = await self.get_run(run_id)
        if run is None:
            raise _RunError(404, "run not found")
        if run.status != "awaiting_confirmation":
            raise _RunError(409, f"run is not awaiting confirmation (status={run.status!r})")
        return await self.execute_run(run_id, Command(resume={"action": "approve"}))

    async def retry(self, run_id: str) -> dict[str, Any]:
        """Re-run a retryable run. Raises if not retryable."""
        run = await self.get_run(run_id)
        if run is None:
            raise _RunError(404, "run not found")
        if run.status != "retryable":
            raise _RunError(409, f"run is not retryable (status={run.status!r})")
        return await self.execute_run(run_id, {"user_request": run.user_request})

    async def list_events(self, run_id: str, after_seq: int = -1) -> list[RunEvent]:
        """Return ordered run events with ``seq > after_seq``."""
        async with self.session_factory() as session:
            result = await session.execute(
                sa.select(RunEvent)
                .where(RunEvent.query_run_id == run_id, RunEvent.seq > after_seq)
                .order_by(RunEvent.seq)
            )
            return list(result.scalars())

    async def _update_status(self, run_id: str, status: str) -> None:
        async with self.session_factory() as session:
            run = await session.get(QueryRun, run_id)
            if run is not None:
                run.status = status
                await session.commit()


class _RunError(Exception):
    """Carries an HTTP status code and detail for the route layer to translate."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def create_run_service(settings: Settings, clock: Clock | None = None) -> tuple[RunService, Any]:
    """Build the engine, session factory, demo graph, object store and service."""
    engine, session_factory = create_engine_and_session()
    resolved_clock = clock or SystemClock()
    graph = build_demo_graph(session_factory, settings, clock=resolved_clock)
    object_store = LocalObjectStore(root=settings.object_store_root)
    service = RunService(session_factory, graph, object_store, settings, clock=resolved_clock)
    return service, engine
