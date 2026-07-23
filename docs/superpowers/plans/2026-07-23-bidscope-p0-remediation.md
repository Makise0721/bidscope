# BidScope P0 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the P0 runtime so a snapshot-backed query produces an evidence-bound online report and idempotent DOCX, subscriptions use that same durable path, and Docker/CI/E2E verify the deployed system.

**Architecture:** Introduce a focused report persistence port that converts a validated graph draft into report/item/claim/citation rows before exporting DOCX. Build the API and scheduler graph with PostgreSQL checkpoints and inject the same `RunService` into subscription execution. Keep `InMemorySaver` for isolated unit tests only. Centralize object-store selection, admin authorization, run idempotency, provenance validation, environment provisioning, and browser-state rendering at their existing boundaries.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, SQLAlchemy/Alembic, PostgreSQL/pgvector, psycopg 3, MinIO/S3, pytest, React 19, TanStack Query, SSE, Vitest, Playwright, Docker Compose.

---

## File Structure

```text
backend/src/bidscope/
├── api/
│   ├── auth.py                         # Admin-token guard shared by production routes
│   ├── dependencies.py                 # Durable graph, report service, storage factory
│   └── routes/
│       ├── runs.py                     # Idempotent create/confirm/retry contract
│       ├── reports.py                  # Evidence/citation-report DTO and DOCX route
│       └── subscriptions.py            # Create only from confirmed scheduled run
├── delivery/
│   ├── objects.py                      # Explicit local/S3 factories and configuration
│   └── reports.py                      # Transactional report/item/claim/citation persistence
├── graph/
│   ├── builder.py                      # GraphDeps includes report persistence port
│   ├── nodes.py                        # Persists online report before DOCX export
│   └── executor.py                     # Idempotent run-key creation and stale pending recovery
├── subscriptions/service.py             # Executes real run/report path and material deltas
├── config.py                            # Object store and auth settings
└── main.py                              # Postgres checkpointer lifespan and stale-run recovery

backend/tests/
├── integration/api/test_runs.py         # Run idempotency, auth, completed report/DOCX
├── integration/test_graph_persistence.py# API/runtime durable resume evidence
├── integration/test_report_delivery.py  # Row/citation persistence and DOCX failure semantics
├── integration/test_subscriptions.py    # Confirmed run gate and cursor-after-report transaction
├── security/test_snapshot_urls.py       # Reject userinfo/non-default HTTPS port
└── unit/delivery/test_objects.py        # Settings-selected local/S3 factory

web/src/
├── api/client.ts                        # Status, report, and SSE client contracts
└── features/workbench/                  # Parsed intent, trace, report evidence, responsive drawer

e2e/
├── global-setup.ts                      # Isolated DB migration/checkpoint/Batch-1 preparation
├── fixtures/test-helper.ts              # Token from process env and deterministic API helpers
└── specs/                               # Six non-conditional P0 browser flows

Dockerfile, compose.yaml, .github/workflows/ci.yml,
README.md, docs/deployment.md             # Canonical process commands and reproducible gates
```

## Task 1: Repair Settings, Provenance, Authentication, and Run Idempotency

**Files:**
- Modify: `backend/src/bidscope/config.py`
- Create: `backend/src/bidscope/api/auth.py`
- Modify: `backend/src/bidscope/domain/snapshots.py`
- Modify: `backend/src/bidscope/api/routes/runs.py`
- Modify: `backend/src/bidscope/api/routes/reports.py`
- Modify: `backend/src/bidscope/api/routes/subscriptions.py`
- Modify: `backend/src/bidscope/api/routes/inbox.py`
- Modify: `backend/src/bidscope/api/routes/sources.py`
- Modify: `backend/src/bidscope/api/routes/evaluations.py`
- Modify: `backend/src/bidscope/graph/executor.py`
- Modify: `backend/tests/security/test_snapshot_urls.py`
- Modify: `backend/tests/integration/api/test_runs.py`
- Create: `backend/tests/integration/api/test_auth_and_idempotency.py`

- [ ] **Step 1: Write failing provenance, authorization, and replay tests**

```python
# backend/tests/security/test_snapshot_urls.py
from datetime import UTC, datetime
from pydantic import ValidationError
import pytest
from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.snapshots import SnapshotManifest


def _manifest(url: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_id": "ccgp-test",
        "source": SourceName.CCGP,
        "capture_kind": CaptureKind.CURATED_PUBLIC_EXCERPT,
        "source_urls": [url],
        "retrieved_at": datetime(2026, 7, 23, tzinfo=UTC),
        "retrieval_outcome": "verified",
        "parser_version": "ccgp-v1",
        "files": {"detail.html": "a" * 64},
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@www.ccgp.gov.cn/cggg/detail.htm",
        "https://www.ccgp.gov.cn:8443/cggg/detail.htm",
    ],
)
def test_manifest_rejects_credential_or_nonstandard_port(url: str) -> None:
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(_manifest(url))
```

```python
# backend/tests/integration/api/test_auth_and_idempotency.py
from fastapi.testclient import TestClient


def test_production_run_route_requires_admin_token(production_settings) -> None:
    from bidscope.main import create_app

    with TestClient(create_app(production_settings)) as client:
        denied = client.post("/api/runs", json={"user_request": "四川服务器采购"})
        allowed = client.post(
            "/api/runs",
            json={"user_request": "四川服务器采购"},
            headers={"X-Admin-Token": "test-admin-token"},
        )
    assert denied.status_code == 401
    assert allowed.status_code == 201


def test_replayed_idempotency_key_returns_same_run(test_client: TestClient) -> None:
    headers = {"Idempotency-Key": "run-replay-1"}
    first = test_client.post("/api/runs", json={"user_request": "四川服务器采购"}, headers=headers)
    second = test_client.post("/api/runs", json={"user_request": "四川服务器采购"}, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
```

- [ ] **Step 2: Run the focused tests and verify expected RED failures**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/security/test_snapshot_urls.py backend/tests/integration/api/test_auth_and_idempotency.py -q
```

Expected: URL cases are accepted and the API either lacks the authentication dependency or creates two distinct runs.

- [ ] **Step 3: Add the minimal settings and auth contracts**

```python
# backend/src/bidscope/config.py additions
from typing import Literal

object_store_type: Literal["local", "s3"] = "local"
s3_endpoint: str | None = None
s3_bucket: str | None = None
s3_access_key: str | None = None
s3_secret_key: str | None = None
s3_prefix: str = ""
```

```python
# backend/src/bidscope/api/auth.py
from fastapi import Header, HTTPException, Request


def require_admin_token(
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> None:
    settings = request.app.state.settings
    if settings.app_mode in {"demo", "test"}:
        return
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")
```

Apply `Depends(require_admin_token)` to every `/api/*` production operation. Keep `/healthz` public and leave `X-Test-Control-Token` independent.

In `SnapshotManifest._validate_provenance`, reject `url.username`, `url.password`, and a parsed port other than `None` or `443` before calling `validate_provenance`.

- [ ] **Step 4: Make run creation replay-safe**

Change `create_run` to accept `run_key: str`, look up an existing `QueryRun` by that value before inserting, and return `(run_id, created)`. Route code derives the key from the non-empty `Idempotency-Key` header, or from a new server-generated UUID when absent:

```python
# backend/src/bidscope/api/routes/runs.py shape
@router.post("", status_code=201)
async def create_run(
    body: CreateRunBody,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    service: RunService = Depends(get_run_service),
    _admin: None = Depends(require_admin_token),
) -> Response:
    run, created = await service.create_run(
        body.user_request.strip(),
        idempotency_key=idempotency_key,
    )
    if created:
        service.schedule_run(run.id, {"user_request": run.user_request})
    return JSONResponse(
        status_code=201 if created else 200,
        content=RunQueryResult.from_row(run).__dict__,
    )
```

Use a unique-constraint recovery path in `create_run` so concurrent requests with the same key return the existing row. Update stale recovery to mark both `pending` and `running` runs as `retryable`.

- [ ] **Step 5: Run focused tests, lint, and type checks**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/security/test_snapshot_urls.py backend/tests/integration/api/test_auth_and_idempotency.py backend/tests/integration/api/test_runs.py -q
uv run ruff check backend/src/bidscope/api backend/src/bidscope/config.py backend/src/bidscope/domain/snapshots.py backend/src/bidscope/graph/executor.py
uv run mypy backend/src/bidscope
```

Expected: all commands exit `0`.

- [ ] **Step 6: Commit the boundary repair**

```bash
git add backend/src/bidscope/config.py backend/src/bidscope/api backend/src/bidscope/domain/snapshots.py backend/src/bidscope/graph/executor.py backend/tests/security/test_snapshot_urls.py backend/tests/integration/api
git commit -m "fix: enforce P0 API and provenance boundaries"
```

## Task 2: Persist Evidence-Backed Reports Before DOCX Export

**Files:**
- Create: `backend/src/bidscope/delivery/reports.py`
- Modify: `backend/src/bidscope/delivery/docx.py`
- Modify: `backend/src/bidscope/graph/builder.py`
- Modify: `backend/src/bidscope/graph/nodes.py`
- Modify: `backend/src/bidscope/api/dependencies.py`
- Modify: `backend/src/bidscope/api/routes/reports.py`
- Modify: `backend/tests/integration/test_report_delivery.py`
- Create: `backend/tests/integration/test_completed_run_delivery.py`
- Modify: `backend/tests/unit/graph/test_report_retry.py`

- [ ] **Step 1: Write failing report persistence tests using real database rows**

```python
# backend/tests/integration/test_completed_run_delivery.py
import pytest
import sqlalchemy as sa
from bidscope.persistence.models import Report, ReportClaim, ReportClaimCitation, ReportItem


@pytest.mark.asyncio
async def test_completed_run_persists_online_report_claims_and_docx(api_client, imported_batch_1, session_factory) -> None:
    created = api_client.post(
        "/api/runs",
        json={"user_request": "四川服务器招标"},
        headers={"Idempotency-Key": "completed-report-1"},
    )
    run_id = created.json()["id"]
    await wait_for_status(api_client, run_id, "completed")

    response = api_client.get(f"/api/reports/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    assert body["items"][0]["claims"]
    assert body["items"][0]["citations"]
    assert api_client.get(f"/api/reports/{run_id}/docx").status_code == 200

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Report)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(ReportItem)) >= 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(ReportClaim)) >= 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(ReportClaimCitation)) >= 1
```

```python
# backend/tests/integration/test_report_delivery.py
class FailingObjectStore:
    def put_bytes(self, key: str, data: bytes) -> str:
        raise OSError("object store unavailable")

    def get_bytes(self, key: str) -> bytes:
        raise FileNotFoundError(key)

    def exists(self, key: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_docx_failure_keeps_online_report_and_is_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    persistence = ReportPersistence(session_factory, FailingObjectStore())
    report = await persistence.persist_online_report(
        _sample_report(), _evidence_by_hash(),
    )

    with pytest.raises(DeliveryError):
        await persistence.export_docx(report)

    async with session_factory() as session:
        stored = await session.scalar(sa.select(ReportModel).where(ReportModel.id == report.id))
    assert stored is not None
    assert stored.docx_object_key is None
```

- [ ] **Step 2: Run delivery tests and verify RED failures**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/integration/test_report_delivery.py backend/tests/integration/test_completed_run_delivery.py backend/tests/unit/graph/test_report_retry.py -q
```

Expected: completed run report endpoint is `404`; no persistence port exists; DOCX export incorrectly tries to create a second report row.

- [ ] **Step 3: Implement `ReportPersistence` with a single online-report transaction**

Create `delivery/reports.py` with a narrowly typed service:

```python
class ReportPersistence:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._store = store

    async def persist_online_report(
        self,
        report: DomainReport,
        evidence_by_hash: Mapping[str, NoticeEvidence],
    ) -> PersistedReport:
        raise NotImplementedError

    async def export_docx(self, persisted: PersistedReport) -> ExportRecord:
        raise NotImplementedError
```

`persist_online_report` must first find a `ReportModel` by `run_id`. If found, return its domain projection. Otherwise, in one `AsyncSession.begin()` transaction:

- insert `ReportModel` with `export_key=f"online:{report.run_id}"`;
- for each domain item, insert `ReportItem` with `notice_version_id=item.notice_id`;
- resolve each citation hash to the existing `NoticeEvidence` row by `(notice_version_id, span_hash)`;
- insert `ReportCitation`, `ReportClaim`, and `ReportClaimCitation` rows;
- reject a missing or foreign-version evidence binding with `DeliveryError` before commit.

Do not use `ReportDelivery.export_report` to create a report row. Refactor it to accept an existing persisted report and only attach `docx_object_key` after successful object write. Use report ID plus `RENDERER_VERSION` for the DOCX key, while the `reports.export_key` remains the stable online report key.

- [ ] **Step 4: Inject report persistence into the graph and build a typed report**

Add `report_persistence: ReportPersistence` to `GraphDeps`. In `persist_and_deliver`:

```python
async def persist_and_deliver(state: Any, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    report = DomainReport(
        run_id=state.run_id,
        generated_at=deps.clock.now(),
        query_conditions=_intent_conditions(state.search_intent),
        freshness_window=state.report.freshness_window,
        source_availability=state.report.source_availability,
        completeness_warning=state.report.completeness_warning,
        items=state.report.items,
    )
    persisted = await deps.report_persistence.persist_online_report(report, state.evidence_by_id)
    try:
        await deps.report_persistence.export_docx(persisted)
    except DeliveryError as error:
        return {
            "status": RunStatus.COMPLETED,
            "errors": [SerializableError(code=error.code, message=str(error), details={"docx_retryable": True})],
            "node_events": [_event(config, state, "persist_and_deliver", "online_report_ready_docx_failed", "degraded")],
        }
    return {
        "status": RunStatus.COMPLETED,
        "node_events": [_event(config, state, "persist_and_deliver", "report_delivered", "ok")],
    }
```

Ensure `RunService.execute_run` writes completed state fields, `search_intent`, errors, token usage, and `completed_at` to `QueryRun`, not only `status`.

- [ ] **Step 5: Expand the report DTO without exposing raw bodies**

`GET /api/reports/{run_id}` must return, per item, bounded `known_fields`, `unknown_fields`, relevance/risk text, source/capture provenance, source-version ID, citations (`evidence_id`, label, offsets, span hash), and claims. Join `ReportItem`, `ReportCitation`, `ReportClaim`, `ReportClaimCitation`, `NoticeEvidence`, `NoticeVersion`, and `SourceNotice` explicitly; never read or render raw snapshot HTML.

- [ ] **Step 6: Run delivery, graph, and API regression tests**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/unit/graph backend/tests/unit/evidence backend/tests/integration/test_report_delivery.py backend/tests/integration/test_completed_run_delivery.py backend/tests/integration/api/test_runs.py -q
```

Expected: one logical report, report items/claims/citations bound to stored evidence, DOCX downloadable, and DOCX failure preserves online report.

- [ ] **Step 7: Commit the report delivery path**

```bash
git add backend/src/bidscope/delivery backend/src/bidscope/graph backend/src/bidscope/api/dependencies.py backend/src/bidscope/api/routes/reports.py backend/tests/integration/test_report_delivery.py backend/tests/integration/test_completed_run_delivery.py backend/tests/unit/graph
git commit -m "fix: persist evidence-backed reports before DOCX export"
```

## Task 3: Use Durable Runtime Checkpoints and Correct Retry Semantics

**Files:**
- Modify: `backend/src/bidscope/api/dependencies.py`
- Modify: `backend/src/bidscope/main.py`
- Modify: `backend/src/bidscope/graph/executor.py`
- Modify: `backend/tests/integration/api/conftest.py`
- Modify: `backend/tests/integration/test_graph_persistence.py`
- Modify: `backend/tests/integration/test_run_recovery.py`
- Create: `backend/tests/integration/api/test_runtime_recovery.py`

- [ ] **Step 1: Write failing API-runtime durability tests**

```python
# backend/tests/integration/api/test_runtime_recovery.py
import pytest
import sqlalchemy as sa
from bidscope.persistence.models import QueryRun, RunEvent


@pytest.mark.asyncio
async def test_lifespan_marks_pending_and_running_runs_retryable(session_factory, test_settings) -> None:
    async with session_factory() as session:
        session.add_all([
            QueryRun(run_key="pending-1", status="pending", user_request="a"),
            QueryRun(run_key="running-1", status="running", user_request="b"),
        ])
        await session.commit()

    async with started_app(test_settings) as client:
        assert client.get("/healthz").status_code == 200

    async with session_factory() as session:
        statuses = (await session.execute(sa.select(QueryRun.status).order_by(QueryRun.run_key))).scalars().all()
    assert statuses == ["retryable", "retryable"]


def test_api_graph_uses_postgres_checkpoint_service(test_client) -> None:
    service = test_client.app.state.run_service
    assert service.checkpointer_kind == "postgres"
```

- [ ] **Step 2: Run the recovery tests and verify RED failures**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py backend/tests/integration/api/test_runtime_recovery.py -q
```

Expected: the app graph identifies as in-memory, and startup leaves `pending`/`running` rows unchanged.

- [ ] **Step 3: Create a lifecycle-owned durable graph service**

Replace `create_run_service()` with an async context-managed factory used by FastAPI lifespan:

```python
@asynccontextmanager
async def create_run_service(settings: Settings, clock: Clock | None = None):
    engine, session_factory = create_engine_and_session(settings)
    async with AsyncPostgresSaver.from_conn_string(_to_plain_dsn(settings.checkpoint_database_url)) as saver:
        graph = build_runtime_graph(session_factory, settings, saver, clock)
        service = RunService(
            session_factory=session_factory,
            graph=graph,
            object_store=create_object_store(settings),
            settings=settings,
            clock=clock or SystemClock(),
            checkpointer_kind="postgres",
        )
        try:
            yield service, engine
        finally:
            await engine.dispose()
```

Do not call `saver.setup()` here; setup remains the explicit CLI/E2E provisioning action. `main.lifespan` calls `mark_stale_runs_retryable()` after creating the service and before serving requests.

- [ ] **Step 4: Make retry resume the persisted graph thread**

For a `retryable` run with `checkpoint_thread_id`, inspect `await self.graph.aget_state(_config(run_id))`. When that state has pending nodes, call `await execute(self.graph, run_id, Command(resume={"action": "retry"}), session_factory=self.session_factory)`. When it is absent or terminal, call `await execute(self.graph, run_id, {"user_request": run.user_request}, session_factory=self.session_factory)`. Do not create a new `QueryRun` or overwrite `run_key`. Persist every final state field in one update helper.

- [ ] **Step 5: Run checkpoint and API integration tests**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run bidscope checkpoints setup
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py backend/tests/integration/api/test_runtime_recovery.py backend/tests/integration/api/test_sse.py -q
```

Expected: API runtime uses PostgreSQL checkpointing; stale runs become retryable; cross-instance resume does not duplicate node events.

- [ ] **Step 6: Commit durable runtime wiring**

```bash
git add backend/src/bidscope/api/dependencies.py backend/src/bidscope/main.py backend/src/bidscope/graph/executor.py backend/tests/integration/api/conftest.py backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py backend/tests/integration/api/test_runtime_recovery.py
git commit -m "fix: run API workflows with durable checkpoints"
```

## Task 4: Run Confirmed Subscriptions Through the Report Path

**Files:**
- Modify: `backend/src/bidscope/subscriptions/service.py`
- Modify: `backend/src/bidscope/subscriptions/scheduler.py`
- Modify: `backend/src/bidscope/api/routes/subscriptions.py`
- Modify: `backend/src/bidscope/api/dependencies.py`
- Modify: `backend/tests/integration/test_subscriptions.py`
- Modify: `backend/tests/integration/test_scheduler_lock.py`
- Modify: `backend/tests/integration/api/test_runs.py`

- [ ] **Step 1: Write failing confirmed-intent and cursor-order tests**

```python
# backend/tests/integration/test_subscriptions.py
@pytest.mark.asyncio
async def test_subscription_requires_completed_confirmed_scheduled_run(api_client) -> None:
    rejected = api_client.post("/api/subscriptions", json={"run_id": "missing"})
    assert rejected.status_code == 404

    unscheduled = await seed_completed_run(search_intent={"schedule": None})
    assert api_client.post("/api/subscriptions", json={"run_id": unscheduled}).status_code == 409

    scheduled = await seed_completed_run(
        search_intent={"schedule": {"cron_expression": "0 9 * * 1", "timezone": "Asia/Shanghai"}}
    )
    created = api_client.post("/api/subscriptions", json={"run_id": scheduled})
    assert created.status_code == 201


class FailingReportRunService:
    async def run_subscription_query(self, subscription_id: str, scheduled_at: datetime) -> str:
        raise DeliveryError("online report persistence failed")


@pytest.mark.asyncio
async def test_seen_cursor_is_unchanged_when_report_persistence_fails(
    session_factory: async_sessionmaker[AsyncSession],
    imported_batches: None,
) -> None:
    subscription_id = await _create_active_subscription(session_factory)
    service = SubscriptionService(
        session_factory=session_factory,
        run_service=FailingReportRunService(),
    )
    await service.run_subscription(subscription_id)
    assert await count_seen_items(session_factory, subscription_id) == 0
    assert await count_inbox_events(session_factory, subscription_id) == 0
```

```python
@pytest.mark.asyncio
async def test_formatting_only_version_change_does_not_emit_material_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    subscription_id = await _create_subscription_with_seen_version(
        session_factory,
        title="四川服务器采购",
        claim_text="预算 800 万元",
    )
    await _append_latest_version(
        session_factory,
        title="  四川 服务器采购  ",
        claim_text="预算   800 万元",
    )
    stats = await SubscriptionService(session_factory=session_factory).run_subscription(subscription_id)
    assert stats["material_changes"] == 0
```

- [ ] **Step 2: Run subscription tests and verify RED failures**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
```

Expected: API accepts arbitrary `intent`, the service invokes `_dummy_graph`, and cursor advancement is not coupled to report persistence.

- [ ] **Step 3: Change subscription creation to use a completed confirmed run**

Replace `CreateSubscriptionBody.intent` with:

```python
class CreateSubscriptionBody(BaseModel):
    run_id: str
```

`SubscriptionService.create_from_run(run_id)` loads `QueryRun`, requires `status == "completed"`, validates `SearchIntent.model_validate(run.search_intent)`, requires non-null schedule, and persists exactly that normalized intent plus next-run timestamp. Preserve the cron/timezone from `SearchIntent.schedule`; do not allow the public API body to override them.

- [ ] **Step 4: Replace `_dummy_graph` with injected real execution**

Inject a `RunService` protocol into `SubscriptionService`. For each trigger:

1. Create an idempotent scheduled run key from subscription ID and time bucket.
2. Execute the actual graph with the persisted normalized intent/user request.
3. Require a persisted online report for the scheduled run.
4. In the subscription transaction, calculate deltas from the report's included latest notice versions.
5. Call `detect_material_changes(previous_view, current_view)` for changed hashes; emit `material_change` only when the returned list is non-empty.
6. Insert inbox events and update `SubscriptionSeenItem` only after report existence is verified and the transaction can commit.

Leave the advisory lock boundary in `run_subscription` unchanged. Remove `_dummy_graph` after tests no longer reference it.

- [ ] **Step 5: Run subscription and API regressions**

Run:

```bash
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py backend/tests/integration/api/test_runs.py -q
```

Expected: arbitrary intent is rejected, valid confirmed scheduled runs create subscriptions, report failure leaves cursor empty, only business-field changes emit material events, and lock behavior remains single-execution.

- [ ] **Step 6: Commit subscription correctness**

```bash
git add backend/src/bidscope/subscriptions backend/src/bidscope/api/routes/subscriptions.py backend/src/bidscope/api/dependencies.py backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py backend/tests/integration/api/test_runs.py
git commit -m "fix: deliver subscriptions through persisted reports"
```

## Task 5: Wire Configured Object Storage and Repair Deployable Image

**Files:**
- Modify: `backend/src/bidscope/delivery/objects.py`
- Modify: `backend/src/bidscope/api/dependencies.py`
- Modify: `backend/src/bidscope/cli.py`
- Modify: `Dockerfile`
- Modify: `compose.yaml`
- Modify: `scripts/postgres-init/01_create_test_db.sql`
- Modify: `alembic.ini`
- Modify: `migrations/env.py`
- Modify: `backend/tests/unit/delivery/test_objects.py`
- Create: `backend/tests/integration/test_deployment_contract.py`
- Modify: `README.md`
- Modify: `docs/deployment.md`

- [ ] **Step 1: Write failing storage-factory and image-contract tests**

```python
# backend/tests/unit/delivery/test_objects.py
from bidscope.api.dependencies import create_object_store
from bidscope.config import Settings
from bidscope.delivery.objects import LocalObjectStore, S3ObjectStore


def test_storage_factory_selects_s3_with_explicit_configuration() -> None:
    settings = Settings(
        object_store_type="s3",
        s3_endpoint="http://minio:9000",
        s3_bucket="bidscope",
        s3_access_key="minio",
        s3_secret_key="minioadmin",
    )
    assert isinstance(create_object_store(settings), S3ObjectStore)


def test_storage_factory_uses_local_root_in_demo(tmp_path) -> None:
    assert isinstance(create_object_store(Settings(object_store_root=str(tmp_path))), LocalObjectStore)
```

```python
# backend/tests/integration/test_deployment_contract.py
from pathlib import Path


def test_image_contains_migration_inputs_and_canonical_api_command() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic.ini migrations/ ./" in dockerfile
    assert 'CMD ["bidscope", "api", "serve"' in dockerfile
```

- [ ] **Step 2: Run focused tests and verify RED failures**

Run:

```bash
uv run pytest backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_deployment_contract.py -q
```

Expected: no object-store factory exists and Dockerfile lacks migration copy/canonical command.

- [ ] **Step 3: Implement configured storage factory and bucket bootstrap**

Add `create_object_store(settings)` that:

- returns `LocalObjectStore(settings.object_store_root)` for `local`;
- validates all S3 fields and builds a boto3 client with `endpoint_url`, access key, secret key, and region `us-east-1` for `s3`;
- calls an explicit `ensure_bucket()` on startup/CLI deployment setup, treating an already-owned bucket as success.

`S3ObjectStore` receives its configured client rather than relying on ambient credentials. CLI snapshot import uses the same factory as the API.

- [ ] **Step 4: Repair Docker/Compose/migration contract**

Update Dockerfile exactly along these lines:

```dockerfile
COPY pyproject.toml uv.lock alembic.ini ./
COPY migrations ./migrations
COPY scripts/postgres-init ./scripts/postgres-init
COPY backend/src/bidscope ./backend/src/bidscope
COPY data ./data
RUN useradd --uid 1000 --create-home bidscope \
    && mkdir -p /app/data/objects \
    && chown -R bidscope:bidscope /app/data
USER bidscope
CMD ["bidscope", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

Use `bidscope api serve` in Compose and documentation. Add a one-shot MinIO initialization service that waits for MinIO and creates `BIDSCOPE_S3_BUCKET`; API/scheduler depend on it. Keep migrations an explicit compose command, now executable in the image.

Use `postgresql+psycopg://` consistently in `.env.example`, CI, Compose sync tooling, Alembic docs, and `migrations/env.py`; eliminate bare `postgresql://` values that select `psycopg2`.

- [ ] **Step 5: Run local contract tests and build/smoke the image**

Run:

```bash
uv run pytest backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_deployment_contract.py -q
docker build -t bidscope:p0-remediation .
docker run --rm bidscope:p0-remediation bidscope api serve --help
```

Expected: tests pass; image builds; CLI prints serve-command help without an option error.

- [ ] **Step 6: Commit deployable storage and image**

```bash
git add backend/src/bidscope/delivery/objects.py backend/src/bidscope/api/dependencies.py backend/src/bidscope/cli.py Dockerfile compose.yaml scripts/postgres-init/01_create_test_db.sql alembic.ini migrations/env.py backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_deployment_contract.py README.md docs/deployment.md
git commit -m "fix: make BidScope storage and image deployable"
```

## Task 6: Render Actual Run State, Intent, Trace, Evidence, and Responsive Drawer

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/features/workbench/Workbench.tsx`
- Modify: `web/src/features/workbench/IntentConfirmation.tsx`
- Modify: `web/src/features/workbench/RunReport.tsx`
- Create: `web/src/features/workbench/RunTimeline.tsx`
- Create: `web/src/features/workbench/EvidenceDrawer.tsx`
- Modify: `web/src/styles/workbench.css`
- Modify: `web/tests/workbench.test.tsx`
- Modify: `web/tests/operations.test.tsx`

- [ ] **Step 1: Write failing state and evidence UI tests**

```tsx
// web/tests/workbench.test.tsx
it("uses the returned status instead of forcing confirmation", async () => {
  server.use(http.post("/api/runs", () => HttpResponse.json({ id: "run-1", status: "completed" })))
  server.use(http.get("/api/reports/run-1", () => HttpResponse.json(reportWithEvidence)))
  render(<App />)

  await userEvent.type(screen.getByLabelText("Enter your request"), "四川服务器招标")
  await userEvent.click(screen.getByRole("button", { name: "Search" }))

  expect(await screen.findByRole("region", { name: "report" })).toBeVisible()
  expect(screen.queryByRole("region", { name: "confirm intent" })).not.toBeInTheDocument()
})

it("renders citation evidence and keeps synthetic URLs as text", async () => {
  render(<RunReport report={reportWithEvidence} />)
  await userEvent.click(screen.getByRole("button", { name: "Open evidence" }))
  expect(screen.getByText("合成演示数据")).toBeVisible()
  expect(screen.getByText("https://example.invalid/demo-001")).toBeVisible()
  expect(screen.queryByRole("link", { name: /example.invalid/ })).not.toBeInTheDocument()
  expect(screen.getByText("预算金额证据")).toBeVisible()
})
```

- [ ] **Step 2: Run Web tests and verify RED failures**

Run:

```bash
npm run test:web -- --run web/tests/workbench.test.tsx web/tests/operations.test.tsx
```

Expected: workbench always renders confirmation, client lacks report event/evidence fields, and no evidence drawer/timeline exists.

- [ ] **Step 3: Add API client contracts and resilient SSE consumption**

Define `RunEvent`, `ReportClaim`, `ReportCitation`, `Evidence`, `ReportProvenance`, and expanded `ReportItem` interfaces matching Task 2 DTOs. Add:

```ts
export function streamRunEvents(
  runId: string,
  afterEventId: string | undefined,
  onEvent: (event: RunEvent) => void,
  onTerminal: (status: string) => void,
): () => void {
  const source = new EventSource(`/api/runs/${runId}/events`)
  source.onmessage = (message) => onEvent(JSON.parse(message.data) as RunEvent)
  source.addEventListener("terminal", (message) => {
    onTerminal((JSON.parse(message.data) as { status: string }).status)
    source.close()
  })
  source.onerror = () => source.close()
  return () => source.close()
}
```

When browser `EventSource` cannot set `Last-Event-ID`, reconnect with a `?after_seq=` query parameter supported by the backend route; update server parsing accordingly.

- [ ] **Step 4: Implement real workbench states and responsive evidence drawer**

- Set initial phase from `createRun` response status.
- Only show `IntentConfirmation` for `awaiting_confirmation`; pass structured intent loaded from run/report DTO rather than hard-coded chips.
- Subscribe to SSE while run is pending/running/awaiting confirmation, append ordered node events, and request report after terminal `completed`.
- Render a desktop trace/evidence right column. At `max-width: 1049px`, move it to an accessible drawer controlled by an icon button with `aria-label="Open evidence"` and fixed dimensions.
- Render source capture kind, retrieval time, hash prefix, freshness, completeness warning, citations and spans. `synthetic_demo` has persistent `合成演示数据` text; `example.invalid` stays a text node.

- [ ] **Step 5: Run Web tests, typecheck, and production build**

Run:

```bash
npm run test:web -- --run
npm run build:web
```

Expected: all tests pass and Vite/TypeScript build exits `0`.

- [ ] **Step 6: Commit the workbench delivery flow**

```bash
git add web/src/api/client.ts web/src/features/workbench web/src/styles/workbench.css web/tests/workbench.test.tsx web/tests/operations.test.tsx
git commit -m "fix: show durable BidScope run evidence in workbench"
```

## Task 7: Provision Non-Conditional P0 E2E and CI Gates

**Files:**
- Create: `e2e/global-setup.ts`
- Modify: `e2e/playwright.config.ts`
- Modify: `e2e/fixtures/test-helper.ts`
- Modify: `e2e/specs/new-query.spec.ts`
- Modify: `e2e/specs/intent-confirmation.spec.ts`
- Modify: `e2e/specs/report-inspection.spec.ts`
- Modify: `e2e/specs/retry-failure.spec.ts`
- Modify: `e2e/specs/subscription-batch.spec.ts`
- Modify: `e2e/specs/docx-download.spec.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `package.json`
- Modify: `README.md`
- Modify: `docs/deployment.md`

- [ ] **Step 1: Write failing E2E setup assertions**

```ts
// e2e/specs/new-query.spec.ts
import { test, expect } from "@playwright/test"

test("creates a non-scheduled query and renders its persisted report", async ({ page }) => {
  await page.goto("/")
  await page.getByLabel("Enter your request").fill("四川服务器招标")
  await page.getByRole("button", { name: "Search" }).click()

  await expect(page.getByRole("region", { name: "report" })).toBeVisible()
  await expect(page.getByText("合成演示数据")).toBeVisible()
  await expect(page.getByRole("button", { name: "Open evidence" })).toBeVisible()
})
```

```ts
// e2e/specs/subscription-batch.spec.ts
await page.getByRole("button", { name: "Save subscription" }).click()
await expect(page.getByText("new_notice")).toBeVisible()
await expect(page.getByText("material_change")).toBeVisible()
```

Do not conditionally skip label, inbox, report, DOCX, or evidence assertions.

- [ ] **Step 2: Run E2E and verify RED failure**

Run:

```bash
npm run test:e2e -- --project=desktop
```

Expected: current web server exits with `No such option: --port` before tests begin.

- [ ] **Step 3: Add isolated Playwright provisioning**

Implement `globalSetup` that:

1. Requires `BIDSCOPE_TEST_CONTROL_TOKEN` generated by the npm script.
2. Runs `uv run alembic upgrade head` with `bidscope_e2e` async/sync URLs.
3. Runs `uv run bidscope checkpoints setup` with the same checkpoint URL.
4. Imports `data/demo/batch-1` through the CLI.
5. Exits nonzero on any command failure.

Configure the web server as:

```ts
command: "npm run build:web && uv run --offline bidscope api serve --host 127.0.0.1 --port 8001",
env: {
  BIDSCOPE_APP_MODE: "test",
  BIDSCOPE_DATABASE_URL: "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_e2e",
  BIDSCOPE_CHECKPOINT_DATABASE_URL: "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_e2e",
  BIDSCOPE_TEST_CONTROL_TOKEN: process.env.BIDSCOPE_TEST_CONTROL_TOKEN!,
},
```

Replace hash URL expectations with BrowserRouter paths, or navigate by visible UI without asserting hash routing. Read the token in `test-helper.ts` from `process.env.BIDSCOPE_TEST_CONTROL_TOKEN`; do not embed a fixed secret.

- [ ] **Step 4: Add view-state and mobile assertions**

For both desktop and mobile projects, assert visible report/evidence state after each workflow. Save screenshots using `expect(page).toHaveScreenshot()` for idle, awaiting confirmation, completed report, and partial-source warning. Add a DOM bounding-box assertion that no main panel overlaps the evidence drawer trigger at `390x844`.

- [ ] **Step 5: Make CI run the actual acceptance gate**

In CI:

- set `BIDSCOPE_CHECKPOINT_DATABASE_URL` to `postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test`;
- add `backend/tests/security` to the unit/security job;
- run `npm ci`, `npm run test:web -- --run`, and `npm run build:web`;
- install Playwright Chromium, provision Postgres test databases, and execute `npm run test:e2e`;
- after image build, start the image using `bidscope api serve`, wait for `/healthz`, run migration/checkpoint commands in the image, and verify the API health endpoint;
- upload Playwright report/screenshots and deterministic evaluation result on failure.

- [ ] **Step 6: Run both browser projects and CI-equivalent local gate**

Run:

```bash
BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e
```

Then run:

```bash
uv run ruff check backend scripts
uv run mypy backend/src/bidscope
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests -q
npm run test:web -- --run
npm run build:web
docker build -t bidscope:p0-remediation .
git diff --check
```

Expected: every command exits `0`; all twelve Playwright tests pass over desktop/mobile; screenshots and reports are produced only as test artifacts.

- [ ] **Step 7: Commit acceptance-gate repair**

```bash
git add e2e .github/workflows/ci.yml package.json README.md docs/deployment.md
git commit -m "test: enforce BidScope P0 deployment and E2E gate"
```

## Task 8: Execute the Clean P0 Release Verification

**Files:**
- Modify only if verification exposes a tested defect in the preceding tasks.
- Generated: `eval/results/deterministic.json` (do not commit; CI uploads it as an artifact)

- [ ] **Step 1: Start clean infrastructure and apply schema**

Run:

```bash
docker compose down -v
docker compose up -d postgres minio minio-init
BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope' uv run alembic upgrade head
BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope' uv run bidscope checkpoints setup
```

Expected: all services become healthy, migration reaches head, and checkpoint setup prints `checkpoint tables ready`.

- [ ] **Step 2: Import controlled P0 data and run backend checks**

Run:

```bash
BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope' uv run bidscope snapshots import data/snapshots/ccgp/2026-07-18-central-open
BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope' uv run bidscope snapshots import data/snapshots/ggzy/2026-07-18-construction
BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope' uv run bidscope snapshots import data/demo/batch-1
uv run ruff check backend scripts
uv run mypy backend/src/bidscope
BIDSCOPE_APP_MODE=test BIDSCOPE_DATABASE_URL='postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test' BIDSCOPE_CHECKPOINT_DATABASE_URL='postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test' uv run pytest backend/tests -q
```

Expected: each import succeeds exactly once; lint/type/tests exit `0`.

- [ ] **Step 3: Run Web, image, Compose, browser, and evaluation gates**

Run:

```bash
npm ci
npm run test:web -- --run
npm run build:web
docker build -t bidscope:p0-remediation .
docker compose up -d api scheduler
curl --fail http://localhost:8000/healthz
BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e
uv run bidscope eval run --mode deterministic --output eval/results/deterministic.json
git diff --check
```

Expected: API health returns JSON with `status: ok`; both process roles stay healthy; all browser flows pass; evaluation records full provenance; diff check is clean.

- [ ] **Step 4: Record the release evidence and commit any test-driven fixes**

If Step 1–3 expose an implementation defect, first add the narrow failing regression test, rerun it to observe RED, implement the smallest correction, then rerun the full affected gate. Do not change evaluation fixture metrics or documentation claims to conceal a failed target.

When verification exposes a regression, return to the task that owns the affected boundary. Add the narrow regression test there, observe its failure, implement the smallest correction, and use that task's explicit commit command. Do not add a speculative catch-all commit during release verification.

## Plan Self-Review

- Spec coverage: Task 1 covers auth, URL policy, and run idempotency; Task 2 covers online report, citations, DOCX semantics; Task 3 covers durable runtime and recovery; Task 4 covers subscription confirmation, real execution, material changes, and cursor transaction order; Task 5 covers storage/image/migration deployment; Task 6 covers actual workbench states/SSE/evidence; Task 7 covers Playwright/CI; Task 8 is the full clean verification gate.
- Placeholder scan: no `TBD`, `TODO`, deferred implementation, or unspecified test instructions remain.
- Type consistency: `ReportPersistence`, `GraphDeps.report_persistence`, `RunService`, `Idempotency-Key`, `X-Admin-Token`, `X-Test-Control-Token`, `postgresql+psycopg`, and `bidscope api serve` use the same names throughout.
