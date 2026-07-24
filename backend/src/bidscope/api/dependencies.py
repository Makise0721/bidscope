"""Application dependencies for the BidScope API.

The API runs a single demo graph (fake deterministic model + hash embeddings)
shared across all requests via ``app.state``. :class:`RunService` wraps that
graph together with the persistence session factory and the object store, and
exposes the run lifecycle operations the routes call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

import sqlalchemy as sa
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from bidscope.clock import Clock, SystemClock
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore, ObjectStore
from bidscope.delivery.reports import ReportPersistence
from bidscope.domain.runs import SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.graph.executor import Command, _to_plain_dsn, create_run, execute
from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
from bidscope.persistence.models import NoticeEvidence, NoticeVersion, QueryRun, RunEvent
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
            evidence_rows = session.execute(
                sa.select(NoticeEvidence.text)
                .where(NoticeEvidence.notice_version_id == version.id)
                .order_by(NoticeEvidence.id)
            )
            views[str(version.id)] = NoticeView(
                source="synthetic_demo",
                external_id=str(version.id),
                canonical_url=f"https://example.invalid/{version.id}",
                project_number=None,
                content_hash=version.content_hash,
                title=version.title,
                purchaser=version.purchaser,
                region=version.region,
                budget_minor_units=version.budget_minor_units,
                budget_currency=version.budget_currency,
                deadline=version.deadline,
                claim_supporting_texts=tuple(evidence_rows.scalars()),
            )
    return views


def _to_sync_dsn(async_url: str) -> str:
    """Convert an ``asyncpg`` DSN to a synchronous ``psycopg`` (v3) DSN."""
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


async def _drain_task_preserving_cancellation[T](task: asyncio.Future[T]) -> T:
    """Drain ``task`` while remembering every cancellation of the caller."""
    current = asyncio.current_task()
    cancellation_count = current.cancelling() if current is not None else 0
    if current is not None:
        for _ in range(cancellation_count):
            current.uncancel()

    try:
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                if current is None:
                    raise
                new_cancellations = current.cancelling()
                cancellation_count += new_cancellations
                for _ in range(new_cancellations):
                    current.uncancel()
    finally:
        if current is not None:
            for _ in range(cancellation_count):
                current.cancel()


def build_demo_graph(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    checkpointer: AsyncPostgresSaver,
    sync_session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
    object_store: ObjectStore | None = None,
) -> Any:
    """Compile the demo workflow with the lifecycle-owned Postgres saver."""
    searcher = HybridSearcher(
        session_factory, HashEmbeddingProvider(dimension=1024),
    )
    notice_factory = sync_session_factory or sessionmaker(
        bind=sa.create_engine(_to_sync_dsn(settings.database_url))
    )
    resolved_store = object_store or LocalObjectStore(root=settings.object_store_root)
    deps = GraphDeps(
        intent_model=FakeIntentModel(),
        duplicate_model=FakeDuplicateModel(),
        report_model=FakeReportModel(),
        searcher=searcher,
        clock=clock or SystemClock(),
        load_notice_views=lambda ids: _load_notice_views(notice_factory, ids),
        report_persistence=ReportPersistence(session_factory, resolved_store),
    )
    return build_graph(deps, checkpointer=checkpointer)


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
        checkpointer_kind: str = "unknown",
    ) -> None:
        self.session_factory = session_factory
        self.graph = graph
        self.object_store = object_store
        self.settings = settings
        self.clock = clock or SystemClock()
        self.checkpointer_kind = checkpointer_kind
        #: Test-only: name of the node that should fail on the next run.
        #: Set by the ``/api/test-controls/fail-next-node`` route and consumed
        #: once by ``execute_run``.
        self.fail_next_node: str | None = None
        self._run_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._shutting_down = False

    async def create_run(
        self, user_request: str, *, run_key: str | None = None
    ) -> tuple[str, bool]:
        """Persist or load a ``pending`` run by its idempotency key."""
        return await create_run(
            user_request, run_key=run_key, session_factory=self.session_factory
        )

    def schedule_run(self, run_id: str, input: Any) -> asyncio.Task[dict[str, Any]]:  # noqa: ANN401
        """Schedule a run and retain it until its task completes."""
        if self._shutting_down:
            raise RuntimeError("run service is shutting down")
        task = asyncio.create_task(self.execute_run(run_id, input))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Cancel and drain all detached runs before shared resources close."""
        self._shutting_down = True
        tasks = tuple(self._run_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._run_tasks.clear()

    async def execute_run(self, run_id: str, input: Any) -> dict[str, Any]:  # noqa: ANN401
        """Drive the graph from ``input`` and sync the final status back to the DB.

        Test-only: when ``self.fail_next_node`` is set, the very next run
        short-circuits with a ``retryable`` failure *before* the graph executes,
        then clears the flag so subsequent runs proceed normally.
        """
        try:
            return await self._execute_run(run_id, input)
        except asyncio.CancelledError:
            error = SerializableError(
                code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                message="run execution cancelled",
                details={},
            ).model_dump(mode="json")
            await self._persist_cancellation(run_id, error)
            raise

    async def _persist_cancellation(
        self,
        run_id: str,
        error: dict[str, Any],
        *,
        expected_status: str | None = None,
    ) -> None:
        """Persist a retryable cancellation without losing the caller's cancel."""
        task = asyncio.create_task(
            self._update_status(
                run_id,
                "retryable",
                error=error,
                expected_status=expected_status,
            )
        )
        current = asyncio.current_task()
        cancellation_count = current.cancelling() if current is not None else 0
        if current is not None:
            for _ in range(cancellation_count):
                current.uncancel()

        deadline = asyncio.get_running_loop().time() + 5
        status_error: BaseException | None = None
        timed_out = False
        try:
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except asyncio.CancelledError:
                    if current is not None:
                        new_cancellations = current.cancelling()
                        cancellation_count += new_cancellations
                        for _ in range(new_cancellations):
                            current.uncancel()
                    continue
                except TimeoutError:
                    timed_out = True
                    break
                except BaseException as error:
                    status_error = error
                    break

            if timed_out and not task.done():
                task.cancel()

            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if current is not None:
                        new_cancellations = current.cancelling()
                        cancellation_count += new_cancellations
                        for _ in range(new_cancellations):
                            current.uncancel()

            while True:
                try:
                    child_result = await asyncio.gather(task, return_exceptions=True)
                except asyncio.CancelledError:
                    if current is not None:
                        new_cancellations = current.cancelling()
                        cancellation_count += new_cancellations
                        for _ in range(new_cancellations):
                            current.uncancel()
                    continue
                break
            child_error = child_result[0]
            if (
                isinstance(child_error, BaseException)
                and not isinstance(child_error, asyncio.CancelledError)
                and status_error is None
            ):
                status_error = child_error
        finally:
            if current is not None:
                for _ in range(cancellation_count):
                    current.cancel()

        if status_error is not None:
            raise asyncio.CancelledError() from status_error

    async def _execute_run(self, run_id: str, input: Any) -> dict[str, Any]:  # noqa: ANN401
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

        run = await self.get_run(run_id)
        checkpoint_thread_id = run.checkpoint_thread_id if run is not None else run_id
        try:
            result = await execute(
                self.graph,
                run_id,
                input,
                session_factory=self.session_factory,
                checkpoint_thread_id=checkpoint_thread_id,
            )
        except Exception as error:  # noqa: BLE001 - detached route task boundary
            serializable_error = SerializableError(
                code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                message=str(error)[:1000],
                details={},
            ).model_dump(mode="json")
            await self._update_status(run_id, "retryable", error=serializable_error)
            return {"status": "retryable", "errors": [serializable_error]}

        status = result.get("status")
        if status:
            await self._update_status(run_id, status, result=result)
        return result

    async def get_run(self, run_id: str) -> QueryRun | None:
        async with self.session_factory() as session:
            return await session.get(QueryRun, run_id)

    async def confirm(self, run_id: str) -> dict[str, Any]:
        """Resume an awaiting-confirmation run. Raises if not confirmable."""
        await self._claim_run_safely(
            run_id,
            "awaiting_confirmation",
            "awaiting confirmation",
            "confirmation claim cancelled",
        )
        return await self.execute_run(run_id, Command(resume={"action": "approve"}))

    async def retry(self, run_id: str) -> dict[str, Any]:
        """Resume a retryable checkpoint or restart the original request."""
        await self._claim_run_safely(
            run_id,
            "retryable",
            "retryable",
            "retry claim cancelled",
        )
        try:
            run = await self.get_run(run_id)
            if run is None:
                raise _RunError(404, "run not found")

            thread_id = run.checkpoint_thread_id or str(run.id)
            get_state = getattr(self.graph, "aget_state", None)
            state = (
                await get_state({"configurable": {"thread_id": thread_id}})
                if get_state
                else None
            )
        except asyncio.CancelledError:
            await self._repair_cancelled_claim(run_id, "retry checkpoint lookup cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - checkpoint recovery boundary
            serializable_error = SerializableError(
                code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                message=str(error)[:1000],
                details={},
            ).model_dump(mode="json")
            await self._update_status(run_id, "retryable", error=serializable_error)
            return {"status": "retryable", "errors": [serializable_error]}
        if state and state.values and state.next:
            return await self.execute_run(run_id, Command(resume={"action": "approve"}))
        return await self.execute_run(run_id, {"user_request": run.user_request})

    async def _claim_run_safely(
        self,
        run_id: str,
        eligible_status: str,
        status_name: str,
        cancellation_message: str,
    ) -> None:
        """Claim a run without leaving a committed claim stranded on cancellation."""
        claim = asyncio.create_task(
            self._claim_run(run_id, eligible_status, status_name),
        )
        try:
            await asyncio.shield(claim)
        except asyncio.CancelledError as cancellation_error:
            try:
                claimed = await _drain_task_preserving_cancellation(claim)
            except BaseException as claim_error:
                raise cancellation_error from claim_error
            if claimed:
                await self._repair_cancelled_claim(run_id, cancellation_message)
            raise

    async def _repair_cancelled_claim(self, run_id: str, message: str) -> None:
        """Restore only the run still owned by this claim to retryable."""
        serializable_error = SerializableError(
            code=BidScopeErrorCode.GRAPH_NODE_ERROR,
            message=message[:1000],
            details={},
        ).model_dump(mode="json")
        await self._persist_cancellation(
            run_id,
            serializable_error,
            expected_status="running",
        )

    async def _claim_run(self, run_id: str, eligible_status: str, status_name: str) -> bool:
        """Atomically move an eligible run to ``running`` or raise a lifecycle error."""
        async with self.session_factory() as session:
            result = await session.execute(
                sa.update(QueryRun)
                .where(QueryRun.id == run_id, QueryRun.status == eligible_status)
                .values(status="running", updated_at=self.clock.now())
                .returning(QueryRun.id)
            )
            claimed_id = result.scalar_one_or_none()
            await session.commit()

        if claimed_id is not None:
            return True

        run = await self.get_run(run_id)
        if run is None:
            raise _RunError(404, "run not found")
        raise _RunError(409, f"run is not {status_name} (status={run.status!r})")

    async def list_events(self, run_id: str, after_seq: int = -1) -> list[RunEvent]:
        """Return ordered run events with ``seq > after_seq``."""
        async with self.session_factory() as session:
            result = await session.execute(
                sa.select(RunEvent)
                .where(RunEvent.query_run_id == run_id, RunEvent.seq > after_seq)
                .order_by(RunEvent.seq)
            )
            return list(result.scalars())

    async def _update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        expected_status: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            if expected_status is not None:
                values: dict[str, Any] = {
                    "status": str(status),
                    "updated_at": self.clock.now(),
                }
                if error is not None:
                    values["error"] = error
                if str(status) == "completed":
                    values["completed_at"] = self.clock.now()
                await session.execute(
                    sa.update(QueryRun)
                    .where(
                        QueryRun.id == run_id,
                        QueryRun.status == expected_status,
                    )
                    .values(**values)
                )
                await session.commit()
                return

            run = await session.get(QueryRun, run_id)
            if run is not None:
                run.status = str(status)
                run.updated_at = self.clock.now()
                if result is not None:
                    intent = result.get("search_intent")
                    if intent is not None:
                        run.search_intent = _json_safe(intent)
                    errors = result.get("errors")
                    if errors:
                        run.error = {"errors": _json_safe(errors)}
                    usage = result.get("token_usage")
                    if usage:
                        run.token_usage = {"calls": _json_safe(usage)}
                if error is not None:
                    run.error = error
                if str(status) == "completed":
                    run.completed_at = self.clock.now()
                await session.commit()


def _json_safe(value: Any) -> Any:
    """Convert graph/Pydantic values to JSON-safe persistence payloads."""
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return _json_safe(value.value)
    return value


class _RunError(Exception):
    """Carries an HTTP status code and detail for the route layer to translate."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@asynccontextmanager
async def create_run_service(
    settings: Settings,
    clock: Clock | None = None,
) -> AsyncIterator[tuple[RunService, Any]]:
    """Own the API's database engines and durable LangGraph checkpointer.

    Checkpoint schema provisioning deliberately remains outside this factory. The
    explicit ``bidscope checkpoints setup`` command creates those tables before
    any API process is started.
    """
    engine, session_factory = create_engine_and_session(settings)
    sync_engine = sa.create_engine(_to_sync_dsn(settings.database_url))
    sync_session_factory = sessionmaker(bind=sync_engine)
    resolved_clock = clock or SystemClock()
    object_store = LocalObjectStore(root=settings.object_store_root)

    try:
        dsn = _to_plain_dsn(settings.checkpoint_database_url)
        async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
            graph = build_demo_graph(
                session_factory,
                settings,
                checkpointer=checkpointer,
                sync_session_factory=sync_session_factory,
                clock=resolved_clock,
                object_store=object_store,
            )
            service = RunService(
                session_factory,
                graph,
                object_store,
                settings,
                clock=resolved_clock,
                checkpointer_kind="postgres",
            )
            yield service, engine
    finally:
        sync_engine.dispose()
        await engine.dispose()
