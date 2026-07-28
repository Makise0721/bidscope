"""Application dependencies for the BidScope API.

The API runs a single demo graph (fake deterministic model + hash embeddings)
shared across all requests via ``app.state``. :class:`RunService` wraps that
graph together with the persistence session factory and the object store, and
exposes the run lifecycle operations the routes call.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, cast

import sqlalchemy as sa
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.clock import Clock, SystemClock
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore, ObjectStore, S3ObjectStore
from bidscope.delivery.reports import ReportPersistence
from bidscope.domain.runs import SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.graph.builder import GraphDeps, build_graph
from bidscope.graph.executor import (
    Command,
    EventReconciliationError,
    FencedCheckpointSaver,
    RunOwnershipLostError,
    _acquire_run_lock,
    _release_run_lock,
    _to_plain_dsn,
    create_run,
    execute,
)
from bidscope.llm.fake import FakeDuplicateModel, FakeIntentModel, FakeReportModel
from bidscope.persistence.models import NoticeEvidence, NoticeVersion, QueryRun, RunEvent
from bidscope.retrieval.embeddings import HashEmbeddingProvider
from bidscope.retrieval.search import HybridSearcher


def create_object_store(settings: Settings) -> ObjectStore:
    """Build the configured object-store backend from ``settings``.

    Single source of truth for both the API (``create_run_service``) and the
    CLI snapshot importer, so the two stay consistent and never construct
    their own stores inline. ``object_store_type='s3'`` is fail-closed: the
    settings validator (:meth:`Settings.validate_s3_storage_requirements`)
    guarantees the S3 connection fields are non-empty before we read them,
    and the store builds its boto3 client from those explicit credentials so
    it never falls back to ambient IAM/env creds.

    Bucket bootstrap (``ensure_bucket``) is the caller's responsibility and is
    intentionally invoked only where a startup-time side effect is wanted
    (e.g. compose's ``minio-init`` creates the bucket out-of-band, so the API
    need not create it on every boot).
    """
    if settings.object_store_type == "s3":
        return S3ObjectStore(
            bucket=settings.s3_bucket or "",
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=(
                settings.s3_access_key.get_secret_value()
                if settings.s3_access_key is not None
                else None
            ),
            aws_secret_access_key=(
                settings.s3_secret_key.get_secret_value()
                if settings.s3_secret_key is not None
                else None
            ),
            region_name=settings.s3_region,
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            max_attempts=settings.s3_max_attempts,
        )
    return LocalObjectStore(root=settings.object_store_root)


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


async def _drain_cancelled_task(task: asyncio.Task[Any]) -> None:
    """Consume an intentionally cancelled child task without cancelling its parent."""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            await task
        else:
            try:
                task.result()
            except asyncio.CancelledError:
                return


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
        session_factory,
        HashEmbeddingProvider(dimension=1024),
    )
    notice_factory = sync_session_factory or sessionmaker(
        bind=sa.create_engine(_to_sync_dsn(settings.database_dsn()))
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
    return build_graph(deps, checkpointer=FencedCheckpointSaver(checkpointer))


@dataclass
class RunQueryResult:
    """Serializable shape of a run returned by the API."""

    id: str
    status: str
    user_request: str

    @classmethod
    def from_row(cls, row: QueryRun) -> RunQueryResult:
        return cls(id=row.id, status=row.status, user_request=row.user_request)


class _ClaimRepairedCancellation(asyncio.CancelledError):
    """Cancellation that already restored a claimed run's retry eligibility."""

    claim_repaired = True


class RunService:
    """Run lifecycle operations over the shared demo graph."""

    _claimed_run_ids: ContextVar[frozenset[tuple[int, str]]] = ContextVar(
        "bidscope_claimed_run_ids",
        default=frozenset(),
    )
    _claimed_run_tokens: ContextVar[frozenset[tuple[int, str, str]]] = ContextVar(
        "bidscope_claimed_run_tokens",
        default=frozenset(),
    )

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
        self._completed_task_errors: deque[BaseException] = deque(maxlen=32)
        self._shutting_down = False
        self._active_run_reservations = 0

    def _try_reserve_run(self) -> bool:
        """Reserve one bounded execution slot without waiting in a request."""
        active = getattr(self, "_active_run_reservations", 0)
        settings = getattr(self, "settings", None)
        limit = getattr(settings, "max_concurrent_runs", 1_000_000)
        if active >= limit:
            return False
        self._active_run_reservations = active + 1
        return True

    def _release_run_reservation(self) -> None:
        active = getattr(self, "_active_run_reservations", 0)
        if active > 0:
            self._active_run_reservations = active - 1

    async def _execute_reserved(
        self, run_id: str, input: Any, *, force_fresh: bool = False
    ) -> dict[str, Any]:  # noqa: ANN401
        try:
            if force_fresh:
                return await self.execute_run(run_id, input, force_fresh=True)
            return await self.execute_run(run_id, input)
        finally:
            self._release_run_reservation()

    def _add_claimed_run_id(self, run_id: str) -> None:
        """Record a relational claim in only the current task's context."""
        self._claimed_run_ids.set(self._claimed_run_ids.get() | {(id(self), run_id)})

    def _remove_claimed_run_id(self, run_id: str) -> None:
        """Drop a relational claim from only the current task's context."""
        self._claimed_run_ids.set(self._claimed_run_ids.get() - {(id(self), run_id)})
        self._claimed_run_tokens.set(
            frozenset(
                claim
                for claim in self._claimed_run_tokens.get()
                if not (claim[0] == id(self) and claim[1] == run_id)
            )
        )

    def _add_claimed_run_token(self, run_id: str, token: str) -> None:
        """Record the token for a claim in only the current task's context."""
        self._claimed_run_tokens.set(
            self._claimed_run_tokens.get() | {(id(self), run_id, token)}
        )

    def _claimed_token(self, run_id: str) -> str | None:
        """Return this task's token for a claimed run, if any."""
        for service_id, claimed_id, token in self._claimed_run_tokens.get():
            if service_id == id(self) and claimed_id == run_id:
                return token
        return None

    async def create_run(
        self,
        user_request: str,
        *,
        run_key: str | None = None,
        audit_context: AuditContext | None = None,
    ) -> tuple[str, bool]:
        """Persist or load a ``pending`` run by its idempotency key."""
        return await create_run(
            user_request,
            run_key=run_key,
            session_factory=self.session_factory,
            audit_context=audit_context,
        )

    def schedule_run(self, run_id: str, input: Any) -> asyncio.Task[dict[str, Any]]:  # noqa: ANN401
        """Schedule a run and retain it until its task completes."""
        if self._shutting_down:
            raise RunCapacityError("run service is shutting down")
        if not self._try_reserve_run():
            raise RunCapacityError("run capacity exhausted")
        try:
            task = asyncio.create_task(self._execute_reserved(run_id, input))
        except BaseException:
            self._release_run_reservation()
            raise
        self._run_tasks.add(task)
        task.add_done_callback(self._on_run_task_done)
        return task

    def _on_run_task_done(self, task: asyncio.Task[dict[str, Any]]) -> None:
        """Consume completed-task exceptions while retaining unexpected failures."""
        self._run_tasks.discard(task)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            self._completed_task_errors.append(error)

    async def shutdown(self) -> None:
        """Cancel and drain all detached runs before shared resources close."""
        self._shutting_down = True
        tasks = tuple(self._run_tasks)
        for task in tasks:
            task.cancel()

        results: list[dict[str, Any] | BaseException] = []
        try:
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)

            failures = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ]
            completed_errors = getattr(self, "_completed_task_errors", ())
            for error in completed_errors:
                if not any(error is failure for failure in failures):
                    failures.append(error)
        finally:
            self._run_tasks.clear()
            completed_errors = getattr(self, "_completed_task_errors", None)
            if completed_errors is not None:
                completed_errors.clear()

        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Detached run tasks failed during shutdown", failures)

    async def execute_run(
        self,
        run_id: str,
        input: Any,
        *,
        force_fresh: bool = False,
    ) -> dict[str, Any]:  # noqa: ANN401
        """Drive the graph from ``input`` and sync the final status back to the DB.

        Test-only: when ``self.fail_next_node`` is set, the very next run
        short-circuits with a ``retryable`` failure *before* the graph executes,
        then clears the flag so subsequent runs proceed normally.
        """
        try:
            return await self._execute_run(
                run_id,
                input,
                force_fresh=force_fresh,
                claimed=(id(self), run_id) in self._claimed_run_ids.get(),
                execution_token=self._claimed_token(run_id),
            )
        except asyncio.CancelledError as cancellation_error:
            if not getattr(cancellation_error, "claim_repaired", False):
                error = SerializableError(
                    code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                    message="run execution cancelled",
                    details={},
                ).model_dump(mode="json")
                await self._persist_cancellation(
                    run_id,
                    error,
                    expected_status="running",
                    execution_token=self._claimed_token(run_id),
                )
            raise
        finally:
            self._remove_claimed_run_id(run_id)

    async def _persist_cancellation(
        self,
        run_id: str,
        error: dict[str, Any],
        *,
        expected_status: str | None = None,
        execution_token: str | None = None,
    ) -> None:
        """Persist a retryable cancellation without losing the caller's cancel."""
        task = asyncio.create_task(
            self._update_status(
                run_id,
                "retryable",
                error=error,
                expected_status=expected_status,
                execution_token=execution_token,
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

    async def _execute_run(
        self,
        run_id: str,
        input: Any,
        *,
        force_fresh: bool = False,
        claimed: bool = False,
        execution_token: str | None = None,
    ) -> dict[str, Any]:  # noqa: ANN401
        if claimed:
            if execution_token is None:
                return {"status": "retryable"}
            self._claimed_run_ids.set(
                self._claimed_run_ids.get() - {(id(self), run_id)}
            )
        else:
            execution_token = await self._start_run_safely(run_id)
            if execution_token is None:
                return {"status": "retryable"}
            self._add_claimed_run_token(run_id, execution_token)

        async with self.session_factory() as owner_session:
            owner_connection = await owner_session.connection()
            lock_acquired = await _acquire_run_lock(owner_connection, run_id)
            if not lock_acquired:
                await self._update_status(
                    run_id,
                    "retryable",
                    expected_status="running",
                    execution_token=execution_token,
                )
                return {"status": "retryable"}

            heartbeat_failure: BaseException | None = None
            heartbeat_repair_attempted = False
            heartbeat_repair_task: asyncio.Task[tuple[bool, dict[str, Any]]] | None = None
            heartbeat_stop = asyncio.Event()

            async def repair_ownership_loss(
                error: BaseException,
            ) -> tuple[bool, dict[str, Any]]:
                """Attempt token-fenced repair and report whether it applied."""
                nonlocal heartbeat_repair_attempted
                if heartbeat_repair_attempted:
                    return False, {
                        "repair_applied": False,
                        "repair_error": "already attempted",
                        "recovery_path": "stale_run_recovery",
                    }
                heartbeat_repair_attempted = True
                serializable_error = SerializableError(
                    code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                    message=str(error)[:1000],
                    details={},
                ).model_dump(mode="json")
                try:
                    applied = await self._update_status(
                        run_id,
                        "retryable",
                        error=serializable_error,
                        expected_status="running",
                        execution_token=execution_token,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as repair_error:
                    return False, {
                        "repair_applied": False,
                        "repair_error": (
                            f"{type(repair_error).__name__}: {str(repair_error)[:1000]}"
                        ),
                        "recovery_path": "stale_run_recovery",
                    }
                if applied:
                    return True, {"repair_applied": True}
                return False, {
                    "repair_applied": False,
                    "repair_error": "token-fenced status update matched no running owner",
                    "recovery_path": "stale_run_recovery",
                }

            async def await_heartbeat_repair() -> tuple[bool, dict[str, Any]]:
                if heartbeat_repair_task is None:
                    return False, {
                        "repair_applied": False,
                        "repair_error": "repair task was not started",
                    }
                try:
                    return await asyncio.shield(heartbeat_repair_task)
                except asyncio.CancelledError:
                    await _drain_task_preserving_cancellation(heartbeat_repair_task)
                    raise

            async def ownership_loss_result(error: BaseException) -> dict[str, Any]:
                """Return a bounded, observable ownership-loss result."""
                if heartbeat_repair_task is None:
                    return {"status": "retryable"}
                _, repair_details = await await_heartbeat_repair()
                serializable_error = SerializableError(
                    code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                    message=str(error)[:1000],
                    details=repair_details,
                ).model_dump(mode="json")
                return {"status": "retryable", "errors": [serializable_error]}

            async def ensure_active(session: AsyncSession | None = None) -> None:
                """Heartbeat this worker only while the run remains running."""
                nonlocal heartbeat_failure
                if heartbeat_failure is not None:
                    raise RunOwnershipLostError(
                        f"run ownership lost: {run_id}"
                    ) from heartbeat_failure
                if session is None:
                    async with self.session_factory() as heartbeat_session:
                        heartbeat_result = await heartbeat_session.execute(
                            sa.update(QueryRun)
                            .where(
                                QueryRun.id == run_id,
                                QueryRun.status == "running",
                                QueryRun.execution_token == execution_token,
                            )
                            .values(updated_at=self.clock.now())
                        )
                        await heartbeat_session.commit()
                else:
                    heartbeat_result = await session.execute(
                        sa.update(QueryRun)
                        .where(
                            QueryRun.id == run_id,
                            QueryRun.status == "running",
                            QueryRun.execution_token == execution_token,
                        )
                        .values(updated_at=self.clock.now())
                    )
                if not bool(getattr(heartbeat_result, "rowcount", 0)):
                    raise RunOwnershipLostError(f"run ownership lost: {run_id}")

            async def heartbeat() -> None:
                nonlocal heartbeat_failure, heartbeat_repair_task
                try:
                    while not heartbeat_stop.is_set():
                        with suppress(TimeoutError):
                            await asyncio.wait_for(
                                heartbeat_stop.wait(),
                                timeout=self.settings.run_heartbeat_seconds,
                            )
                        if heartbeat_stop.is_set():
                            return
                        try:
                            await ensure_active()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as error:
                            heartbeat_failure = error
                            heartbeat_repair_task = asyncio.create_task(
                                repair_ownership_loss(error)
                            )
                            try:
                                await asyncio.shield(heartbeat_repair_task)
                            except asyncio.CancelledError:
                                await _drain_task_preserving_cancellation(heartbeat_repair_task)
                                raise
                            return
                except asyncio.CancelledError:
                    raise

            heartbeat_task: asyncio.Task[None] | None = None
            try:
                try:
                    await ensure_active()
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    heartbeat_failure = error
                    heartbeat_repair_task = asyncio.create_task(
                        repair_ownership_loss(error)
                    )
                    return await ownership_loss_result(error)
                heartbeat_task = asyncio.create_task(heartbeat())
                fail_node = self.fail_next_node
                if fail_node is not None:
                    self.fail_next_node = None
                    injected_error = SerializableError(
                        code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                        message=f"Test-only injected failure for node {fail_node!r}",
                        details={"node": fail_node},
                    ).model_dump(mode="json")
                    result = {"status": "retryable", "errors": [injected_error]}
                    if not await self._update_status(
                        run_id,
                        "retryable",
                        error=injected_error,
                        expected_status="running",
                        execution_token=execution_token,
                    ):
                        return {"status": "retryable"}
                    return result

                run = await self.get_run(run_id)
                checkpoint_thread_id = run.checkpoint_thread_id if run is not None else run_id
                try:
                    result = await execute(
                        self.graph,
                        run_id,
                        input,
                        session_factory=self.session_factory,
                        checkpoint_thread_id=checkpoint_thread_id,
                        force_fresh=force_fresh,
                        ensure_active=ensure_active,
                        execution_token=execution_token,
                    )
                except RunOwnershipLostError as error:
                    return await ownership_loss_result(error)
                except EventReconciliationError as error:
                    serializable_error = SerializableError(
                        code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                        message=str(error)[:1000],
                        details={},
                    ).model_dump(mode="json")
                    await self._update_status(
                        run_id,
                        "retryable",
                        error=serializable_error,
                        expected_status="running",
                        execution_token=execution_token,
                    )
                    return {"status": "retryable", "errors": [serializable_error]}
                except Exception as error:  # noqa: BLE001 - detached route task boundary
                    serializable_error = SerializableError(
                        code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                        message=str(error)[:1000],
                        details={},
                    ).model_dump(mode="json")
                    if not await self._update_status(
                        run_id,
                        "retryable",
                        error=serializable_error,
                        expected_status="running",
                        execution_token=execution_token,
                    ):
                        return {"status": "retryable"}
                    return {"status": "retryable", "errors": [serializable_error]}

                status = result.get("status")
                if status:
                    try:
                        await ensure_active()
                    except RunOwnershipLostError as error:
                        return await ownership_loss_result(error)
                    result_error = None
                    errors = result.get("errors")
                    if errors:
                        result_error = {"errors": _json_safe(errors)}
                    if not await self._update_status(
                        run_id,
                        cast(str, status),
                        error=result_error,
                        result=result,
                        expected_status="running",
                        execution_token=execution_token,
                    ):
                        return {"status": "retryable"}
                return result
            finally:
                heartbeat_stop.set()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await _drain_cancelled_task(heartbeat_task)
                release = asyncio.create_task(_release_run_lock(owner_connection, run_id))
                await _drain_task_preserving_cancellation(release)

    async def _start_run_safely(self, run_id: str) -> str | None:
        """Shield a committed start claim and repair it when cancellation wins."""
        claim = asyncio.create_task(self._start_run(run_id))
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError as cancellation_error:
            try:
                token = await _drain_task_preserving_cancellation(claim)
            except BaseException as claim_error:
                raise cancellation_error from claim_error
            if token is not None:
                repair = asyncio.create_task(
                    self._repair_claim_with_token(run_id, "run start cancelled", str(token))
                )
                try:
                    await _drain_task_preserving_cancellation(repair)
                except BaseException as repair_error:
                    raise cancellation_error from repair_error
            raise cancellation_error

    async def _start_run(self, run_id: str) -> str | None:
        """Atomically activate a pending run and return its fresh ownership token."""
        async with self.session_factory() as session:
            result = await session.execute(
                sa.update(QueryRun)
                .where(QueryRun.id == run_id, QueryRun.status == "pending")
                .values(
                    status="running",
                    execution_token=sa.cast(sa.func.gen_random_uuid(), sa.Text),
                    updated_at=self.clock.now(),
                )
                .returning(QueryRun.execution_token)
            )
            claimed_token = result.scalar_one_or_none()
            await session.commit()
            return str(claimed_token) if claimed_token is not None else None

    async def get_run(self, run_id: str) -> QueryRun | None:
        async with self.session_factory() as session:
            return await session.get(QueryRun, run_id)

    async def confirm(self, run_id: str) -> dict[str, Any]:
        """Resume an awaiting-confirmation run. Raises if not confirmable."""
        if not self._try_reserve_run():
            raise RunCapacityError()
        handed_off = False
        try:
            token = await self._claim_run_safely(
                run_id,
                "awaiting_confirmation",
                "awaiting confirmation",
                "confirmation claim cancelled",
                audit_event_type=AuditEventType.RUN_CONFIRMED,
            )
            self._add_claimed_run_id(run_id)
            self._add_claimed_run_token(run_id, token)
            handed_off = True
            return await self._execute_reserved(
                run_id, Command(resume={"action": "approve"})
            )
        finally:
            if not handed_off:
                self._release_run_reservation()

    async def retry(self, run_id: str) -> dict[str, Any]:
        """Resume a retryable checkpoint or restart the original request."""
        if not self._try_reserve_run():
            raise RunCapacityError()
        released = False
        try:
            token = await self._claim_run_safely(
                run_id,
                "retryable",
                "retryable",
                "retry claim cancelled",
                audit_event_type=AuditEventType.RUN_RETRIED,
            )
            self._add_claimed_run_id(run_id)
            self._add_claimed_run_token(run_id, token)
            result = await self._retry_after_claim_safely(run_id)
            self._release_run_reservation()
            released = True
            return result
        finally:
            if not released:
                self._release_run_reservation()

    async def _retry_after_claim_safely(self, run_id: str) -> dict[str, Any]:
        """Look up retry state and repair the claim when cancellation interrupts it."""
        try:
            context = await self._retry_context(run_id)
        except asyncio.CancelledError as cancellation_error:
            if getattr(cancellation_error, "claim_repaired", False):
                self._remove_claimed_run_id(run_id)
                raise
            repair = asyncio.create_task(
                self._repair_claim_with_token(
                    run_id,
                    "retry checkpoint lookup cancelled",
                    self._claimed_token(run_id),
                )
            )
            try:
                await _drain_task_preserving_cancellation(repair)
            except BaseException as repair_error:
                self._remove_claimed_run_id(run_id)
                raise cancellation_error from repair_error
            self._remove_claimed_run_id(run_id)
            raise

        if isinstance(context, dict):
            self._remove_claimed_run_id(run_id)
            return context
        run, state = context
        if state and state.next:
            return await self.execute_run(
                run_id,
                Command(resume={"action": "retry"}),
                force_fresh=False,
            )
        return await self.execute_run(
            run_id,
            {"user_request": run.user_request},
            force_fresh=True,
        )

    async def _retry_context(self, run_id: str) -> tuple[QueryRun, Any] | dict[str, Any]:
        """Load checkpoint state and persist ordinary lookup failures."""
        try:
            run = await self.get_run(run_id)
            if run is None:
                raise _RunError(404, "run not found")

            thread_id = run.checkpoint_thread_id or str(run.id)
            get_state = getattr(self.graph, "aget_state", None)
            state = (
                await get_state({"configurable": {"thread_id": thread_id}}) if get_state else None
            )
        except Exception as error:  # noqa: BLE001 - checkpoint recovery boundary
            serializable_error = SerializableError(
                code=BidScopeErrorCode.GRAPH_NODE_ERROR,
                message=str(error)[:1000],
                details={},
            ).model_dump(mode="json")
            status_update = asyncio.create_task(
                self._update_status(
                    run_id,
                    "retryable",
                    error=serializable_error,
                    expected_status="running",
                    execution_token=self._claimed_token(run_id),
                )
            )
            try:
                await asyncio.shield(status_update)
            except asyncio.CancelledError as cancellation_error:
                try:
                    applied = await _drain_task_preserving_cancellation(status_update)
                except BaseException as status_error:
                    repair = asyncio.create_task(
                        self._repair_claim_with_token(
                            run_id,
                            "retry checkpoint lookup cancelled",
                            self._claimed_token(run_id),
                        )
                    )
                    try:
                        await _drain_task_preserving_cancellation(repair)
                    except BaseException as repair_error:
                        raise cancellation_error from repair_error
                    raise _ClaimRepairedCancellation(str(cancellation_error)) from status_error
                if not applied:
                    repair = asyncio.create_task(
                        self._repair_claim_with_token(
                            run_id,
                            "retry checkpoint lookup cancelled",
                            self._claimed_token(run_id),
                        )
                    )
                    try:
                        await _drain_task_preserving_cancellation(repair)
                    except BaseException as repair_error:
                        raise cancellation_error from repair_error
                raise _ClaimRepairedCancellation(str(cancellation_error)) from None
            return {"status": "retryable", "errors": [serializable_error]}
        return run, state

    async def _claim_run_safely(
        self,
        run_id: str,
        eligible_status: str,
        status_name: str,
        cancellation_message: str,
        audit_event_type: AuditEventType | None = None,
    ) -> str:
        """Claim a run without leaving a committed token stranded on cancellation."""
        claim_runner = self._claim_run
        if audit_event_type is None or len(inspect.signature(claim_runner).parameters) < 4:
            claim = asyncio.create_task(
                claim_runner(run_id, eligible_status, status_name)
            )
        else:
            claim = asyncio.create_task(
                claim_runner(run_id, eligible_status, status_name, audit_event_type)
            )
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError as cancellation_error:
            try:
                token = await _drain_task_preserving_cancellation(claim)
            except BaseException as claim_error:
                raise cancellation_error from claim_error
            if token:
                self._add_claimed_run_token(run_id, str(token))
                repair = asyncio.create_task(
                    self._repair_claim_with_token(run_id, cancellation_message, str(token)),
                )
                try:
                    await _drain_task_preserving_cancellation(repair)
                except BaseException as repair_error:
                    raise cancellation_error from repair_error
                finally:
                    self._remove_claimed_run_id(run_id)
            raise cancellation_error

    async def _repair_claim_with_token(
        self,
        run_id: str,
        message: str,
        execution_token: str | None,
    ) -> None:
        """Call the repair hook with a token while tolerating legacy test hooks."""
        repair = self._repair_cancelled_claim
        if len(inspect.signature(repair).parameters) < 3:
            await repair(run_id, message)
        else:
            await repair(run_id, message, execution_token)

    async def _repair_cancelled_claim(
        self,
        run_id: str,
        message: str,
        execution_token: str | None = None,
    ) -> None:
        """Restore only the run still owned by this claim to retryable."""
        execution_token = execution_token or self._claimed_token(run_id)
        serializable_error = SerializableError(
            code=BidScopeErrorCode.GRAPH_NODE_ERROR,
            message=message[:1000],
            details={},
        ).model_dump(mode="json")
        await self._persist_cancellation(
            run_id,
            serializable_error,
            expected_status="running",
            execution_token=execution_token,
        )

    async def _claim_run(
        self,
        run_id: str,
        eligible_status: str,
        status_name: str,
        audit_event_type: AuditEventType | None = None,
    ) -> str:
        """Atomically move an eligible run to ``running`` with a fresh token."""
        async with self.session_factory() as session:
            result = await session.execute(
                sa.update(QueryRun)
                .where(QueryRun.id == run_id, QueryRun.status == eligible_status)
                .values(
                    status="running",
                    execution_token=sa.cast(sa.func.gen_random_uuid(), sa.Text),
                    updated_at=self.clock.now(),
                )
                .returning(QueryRun.execution_token)
            )
            claimed_token = result.scalar_one_or_none()
            if claimed_token is not None and audit_event_type is not None:
                await record_audit_event(
                    session,
                    AuditContext(
                        method="POST",
                        path=f"/api/runs/{run_id}/{audit_event_type.value.rsplit('.', 1)[-1]}",
                        run_id=run_id,
                    ),
                    audit_event_type,
                    AuditOutcome.SUCCESS,
                    {"status": "running"},
                )
            await session.commit()

        if claimed_token is not None:
            return str(claimed_token)

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
        execution_token: str | None = None,
    ) -> bool:
        if expected_status == "running" and execution_token is None:
            return False
        async with self.session_factory() as session:
            if expected_status is not None:
                values: dict[str, Any] = {
                    "status": str(status),
                    "updated_at": self.clock.now(),
                }
                if result is not None:
                    intent = result.get("search_intent")
                    if intent is not None:
                        values["search_intent"] = _json_safe(intent)
                    errors = result.get("errors")
                    if errors:
                        values["error"] = {"errors": _json_safe(errors)}
                    elif str(status) == "completed" and error is None:
                        values["error"] = None
                    usage = result.get("token_usage")
                    if usage:
                        values["token_usage"] = {"calls": _json_safe(usage)}
                if error is not None:
                    values["error"] = error
                if str(status) == "completed":
                    values["completed_at"] = self.clock.now()
                if str(status) != "running":
                    values["execution_token"] = None
                predicates = [
                    QueryRun.id == run_id,
                    QueryRun.status == expected_status,
                ]
                if execution_token is not None:
                    predicates.append(QueryRun.execution_token == execution_token)
                update_result = await session.execute(
                    sa.update(QueryRun).where(*predicates).values(**values)
                )
                await session.commit()
                return bool(getattr(update_result, "rowcount", 0))

            run = await session.get(QueryRun, run_id)
            if run is not None:
                run.status = str(status)
                run.updated_at = self.clock.now()
                if str(status) != "running":
                    run.execution_token = None
                if result is not None:
                    intent = result.get("search_intent")
                    if intent is not None:
                        run.search_intent = _json_safe(intent)
                    errors = result.get("errors")
                    if errors:
                        run.error = {"errors": _json_safe(errors)}
                    elif str(status) == "completed":
                        run.error = None
                    usage = result.get("token_usage")
                    if usage:
                        run.token_usage = {"calls": _json_safe(usage)}
                if error is not None:
                    run.error = error
                if str(status) == "completed":
                    run.completed_at = self.clock.now()
                await session.commit()
                return True
            return False


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


class RunCapacityError(Exception):
    """Bounded error raised when no execution slot is immediately available."""

    status_code = 429
    code = "run_capacity_exhausted"

    def __init__(self, message: str = "run capacity exhausted") -> None:
        super().__init__(message)


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
) -> AsyncIterator[tuple[RunService, Any, AsyncPostgresSaver]]:
    """Own the API's database engines and durable LangGraph checkpointer.

    Checkpoint schema provisioning deliberately remains outside this factory. The
    explicit ``bidscope checkpoints setup`` command creates those tables before
    any API process is started.
    """
    engine, session_factory = create_engine_and_session(settings)
    sync_engine = sa.create_engine(_to_sync_dsn(settings.database_dsn()))
    sync_session_factory = sessionmaker(bind=sync_engine)
    resolved_clock = clock or SystemClock()
    object_store = create_object_store(settings)

    try:
        dsn = _to_plain_dsn(settings.checkpoint_database_dsn())
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
            yield service, engine, checkpointer
    finally:
        sync_engine.dispose()
        await engine.dispose()


def _build_run_service_components(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    sync_session_factory: sessionmaker[Session],
    object_store: ObjectStore,
    clock: Clock,
    checkpointer: AsyncPostgresSaver,
) -> RunService:
    """Compile the demo graph over a pre-built checkpointer and assemble a
    :class:`RunService`.

    Shared between the API's :func:`create_run_service` and the subscription
    scheduler's process-local assembly so the two stay consistent without
    coupling them through ``app.state``.
    """
    graph = build_demo_graph(
        session_factory,
        settings,
        checkpointer=checkpointer,
        sync_session_factory=sync_session_factory,
        clock=clock,
        object_store=object_store,
    )
    return RunService(
        session_factory,
        graph,
        object_store,
        settings,
        clock=clock,
        checkpointer_kind="postgres",
    )
