# BidScope P1-B Observability and Runtime Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add request/run-correlated structured observability, dependency readiness checks, bounded metrics, explicit connection/timeout settings, concurrency control, and graceful shutdown without adding a new long-running infrastructure service.

**Architecture:** Use a small standard-library observability layer rather than an external APM. `backend/src/bidscope/observability.py` owns the request middleware, bounded request context, JSON log records, process-local metrics registry, and timing helpers; `backend/src/bidscope/api/health.py` owns readiness probes. Runtime limits are configured through `Settings`, enforced in `RunService`, and covered by failure-oriented tests.

**Tech Stack:** Python 3.12, FastAPI middleware/responses, SQLAlchemy async engine, boto3 botocore `Config`, LangGraph Postgres saver, pytest/pytest-asyncio, Docker Compose, Playwright.

---

## File Map

**Create**

- `backend/src/bidscope/observability.py` — request context, JSON formatter, redaction allowlist, bounded metrics registry, and timing helpers.
- `backend/src/bidscope/api/health.py` — readiness probe protocol, dependency checks, and sanitized response model.
- `backend/tests/unit/test_observability.py` — request IDs, JSON log shape, redaction, and metric rendering.
- `backend/tests/unit/test_readiness.py` — individual dependency state mapping and timeout behavior.
- `backend/tests/integration/api/test_readiness.py` — `/readyz` against working and failing database/object/checkpoint probes.
- `backend/tests/integration/test_run_capacity.py` — max-concurrent-run behavior and recovery after capacity is released.

**Modify**

- `backend/src/bidscope/config.py` — pool, timeout, concurrency, request-size, SSE, shutdown, and scheduler observability settings.
- `backend/src/bidscope/db.py` — pass SQLAlchemy pool and connect/command timeout options.
- `backend/src/bidscope/delivery/objects.py` — configure boto3 connect/read timeout and bounded retries.
- `backend/src/bidscope/api/dependencies.py` — expose runtime metrics and readiness probes through `RunService`/app state; add capacity semaphore.
- `backend/src/bidscope/observability.py` — request context middleware, JSON logging, bounded metrics, and timing helpers; no separate middleware module is created.
- `backend/src/bidscope/main.py` — install middleware, add `/readyz` and protected `/metrics`, and wire shutdown ordering.
- `backend/src/bidscope/api/routes/events.py` — emit SSE connection lifecycle metrics and use request context.
- `backend/src/bidscope/api/routes/runs.py` — map capacity exhaustion to bounded 429/retryable behavior.
- `backend/src/bidscope/subscriptions/scheduler.py` — tick metrics, last-success timestamp, timeout, and warning logs.
- `backend/src/bidscope/graph/executor.py` — node timing/error metric hooks without changing ownership fencing.
- `backend/src/bidscope/delivery/reports.py` and `backend/src/bidscope/delivery/docx.py` — delivery timing/error metrics.
- `backend/src/bidscope/snapshots/importer.py` — import counters, warning counts, and duration metrics.
- `backend/tests/unit/test_cli.py`, `backend/tests/unit/test_health.py`, `backend/tests/unit/subscriptions/test_scheduler.py` — settings, health, and scheduler regression coverage.
- `backend/tests/integration/conftest.py` — close/reset metrics and include P1 runtime state in isolation.
- `Dockerfile`, `compose.yaml`, `.github/workflows/ci.yml` — readiness healthcheck, bounded stop settings, smoke and runtime gates.
- `README.md`, `docs/deployment.md` — runtime limits, readiness semantics, log fields, metrics access, and scheduler diagnosis.

---

### Task 1: Add explicit runtime settings and engine pool configuration

**Files:** `backend/tests/unit/test_cli.py`, `backend/src/bidscope/config.py`, `backend/src/bidscope/db.py`

- [ ] **Step 1: Write failing settings tests.** Add assertions for default pool values and validation that pool size, overflow, timeout, request size, concurrency, SSE limit, and shutdown seconds are positive. Assert a timeout is not larger than the configured shutdown window when the settings contract disallows that combination.

```python
def test_runtime_limits_have_positive_defaults() -> None:
    settings = Settings()
    assert settings.db_pool_size > 0
    assert settings.max_concurrent_runs > 0
    assert settings.s3_read_timeout_seconds > 0


def test_runtime_limits_reject_non_positive_values() -> None:
    with pytest.raises(ValidationError):
        Settings(db_pool_size=0)
    with pytest.raises(ValidationError):
        Settings(max_concurrent_runs=0)
```

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_cli.py -q`

Expected: FAIL because the new fields do not exist.

- [ ] **Step 3: Implement the settings fields.** Add `db_pool_size=5`, `db_max_overflow=5`, `db_pool_recycle_seconds=1800`, `db_connect_timeout_seconds=5`, `db_command_timeout_seconds=30`, `s3_connect_timeout_seconds=5`, `s3_read_timeout_seconds=30`, `s3_max_attempts=3`, `max_concurrent_runs=2`, `max_request_body_bytes=65536`, `max_sse_connections=10`, `max_report_items=100`, `graceful_shutdown_seconds=30`, and `scheduler_tick_timeout_seconds=60`, each with Pydantic positive bounds.

- [ ] **Step 4: Pass database settings to SQLAlchemy.** Update `create_engine_and_session` to pass `pool_size`, `max_overflow`, `pool_recycle`, `pool_pre_ping=True`, and `connect_args={"timeout": resolved.db_connect_timeout_seconds, "command_timeout": resolved.db_command_timeout_seconds}` to `create_async_engine`. Keep SQLite/test doubles compatible by only passing PostgreSQL-specific connect args when the URL uses `asyncpg`.

- [ ] **Step 5: Run tests and type checks.**

Run: `uv run pytest backend/tests/unit/test_cli.py -q && uv run ruff check backend/src/bidscope/config.py backend/src/bidscope/db.py backend/tests/unit/test_cli.py`

Expected: PASS and exit 0.

- [ ] **Step 6: Commit.**

```bash
git add backend/src/bidscope/config.py backend/src/bidscope/db.py backend/tests/unit/test_cli.py
git commit -m "feat: configure bounded production runtime limits"
```

### Task 2: Implement JSON request context and sensitive-field redaction

**Files:** `backend/src/bidscope/observability.py`, `backend/src/bidscope/main.py`, `backend/tests/unit/test_observability.py`

- [ ] **Step 1: Write failing context/log tests.** Test that a valid `X-Request-ID` is echoed, an absent ID receives a UUID, an invalid/oversized/control-character ID receives a new UUID, and the JSON formatter emits `request_id`, method, normalized path, status, duration, and exception type. Test that token-like keys and values are redacted.

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_observability.py -q`

Expected: FAIL because the observability module and middleware do not exist.

- [ ] **Step 3: Implement bounded request context.** Add a `ContextVar[RequestContext | None]`, `get_request_context()`, `valid_request_id()`, and an ASGI/FastAPI middleware. The middleware must start a monotonic timer, set the context, call the application, add `X-Request-ID`, and log only allowlisted fields. Always clear the context in `finally`.

- [ ] **Step 4: Implement JSON logging.** Add a formatter that serializes one JSON object per line to stdout/stderr, truncates strings at 1000 characters, limits nested details to two levels, and replaces keys matching `token`, `authorization`, `cookie`, `secret`, `api_key`, or `password` with `"[REDACTED]"`.

- [ ] **Step 5: Install middleware and test with TestClient.** Register the middleware in `create_app` before routers. Add a test that calls `/healthz`, checks the response header, and captures a parseable JSON log record.

- [ ] **Step 6: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_observability.py backend/tests/unit/test_health.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/observability.py backend/src/bidscope/main.py backend/tests/unit/test_observability.py backend/tests/unit/test_health.py
git commit -m "feat: add correlated structured request logging"
```

### Task 3: Add bounded metrics registry and instrumentation hooks

**Files:** `backend/src/bidscope/observability.py`, `backend/src/bidscope/main.py`, `backend/src/bidscope/api/routes/events.py`, `backend/src/bidscope/graph/executor.py`, `backend/src/bidscope/subscriptions/scheduler.py`, `backend/src/bidscope/snapshots/importer.py`, `backend/src/bidscope/delivery/reports.py`, `backend/src/bidscope/delivery/docx.py`, `backend/tests/unit/test_observability.py`

- [ ] **Step 1: Write failing metric tests.** Assert that counters accept only predefined metric names and bounded label values, histograms render count/sum/buckets, unknown labels are rejected, and the output is valid Prometheus text without secrets or arbitrary IDs in labels.

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_observability.py -q`

Expected: FAIL because the registry does not exist.

- [ ] **Step 3: Implement the registry.** Define `MetricsRegistry.counter(name, labels)`, `observe(name, value, labels)`, and `render_prometheus()`. Pre-register bounded names: `bidscope_http_requests_total`, `bidscope_http_request_duration_seconds`, `bidscope_runs_total`, `bidscope_run_node_duration_seconds`, `bidscope_run_failures_total`, `bidscope_sse_connections`, `bidscope_scheduler_ticks_total`, `bidscope_snapshot_imports_total`, `bidscope_report_delivery_duration_seconds`, and dependency failure counters. Labels may only be status, node, error_code, source, outcome, or reason from bounded vocabularies; never use run IDs, request IDs, URLs, titles, or user text.

- [ ] **Step 4: Instrument request and lifecycle boundaries.** Increment HTTP counters in middleware; add node timing/error hooks around graph node execution; add scheduler due/ran/skipped/failed counters; add snapshot import and report/DOCX timing; increment SSE open/close metrics in the events route. Keep metric failures non-fatal and log a redacted warning.

- [ ] **Step 5: Add protected metrics route.** Add `GET /metrics` after P1-A auth is available. Return `Response(content=registry.render_prometheus(), media_type="text/plain; version=0.0.4")` and use the existing Admin Token dependency.

- [ ] **Step 6: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_observability.py backend/tests/unit/subscriptions/test_scheduler.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/observability.py backend/src/bidscope/main.py backend/src/bidscope/api/routes/events.py backend/src/bidscope/graph/executor.py backend/src/bidscope/subscriptions/scheduler.py backend/src/bidscope/snapshots/importer.py backend/src/bidscope/delivery/reports.py backend/src/bidscope/delivery/docx.py backend/tests/unit/test_observability.py
git commit -m "feat: expose bounded runtime metrics"
```

### Task 4: Implement readiness probes with sanitized dependency states

**Files:** `backend/src/bidscope/api/health.py`, `backend/src/bidscope/main.py`, `backend/src/bidscope/api/dependencies.py`, `backend/tests/unit/test_readiness.py`, `backend/tests/integration/api/test_readiness.py`

- [ ] **Step 1: Write failing probe tests.** Define fake async probes for database, checkpoint, object store, and configuration. Assert a successful probe returns `{"status": "ok"}` per dependency; a timeout returns `"failed"` with an error code only; an exception message, DSN, bucket, or host never appears in the response.

- [ ] **Step 2: Run tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_readiness.py -q`

Expected: FAIL because the readiness module does not exist.

- [ ] **Step 3: Implement the probe service.** Add a `ReadinessProbe` class with `async check(settings, session_factory, checkpointer, object_store)`. Use a separate bounded timeout for each check. Database uses `SELECT 1`; checkpoint calls `await checkpointer.aget_tuple({"configurable": {"thread_id": "__bidscope_readiness__"}})` as a read-only probe; S3 uses `head_bucket`, while local storage verifies its root exists and is writable. Map all exceptions to `failed` plus a stable code such as `database_unavailable`, `checkpoint_unavailable`, or `object_store_unavailable`.

- [ ] **Step 4: Wire `/readyz`.** Store the probe dependencies in `app.state` during lifespan, add the route to `create_app`, and return HTTP 200 only when all required checks are `ok`; return HTTP 503 with the same bounded JSON shape otherwise. Keep `/healthz` unchanged as a process liveness endpoint.

- [ ] **Step 5: Add integration tests.** Against the integration database, assert `/readyz` is 200 when all dependencies are available and 503 when the injected probe reports a database or object-store failure. Verify the response contains no DSN, endpoint, stack trace, or credentials.

- [ ] **Step 6: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_readiness.py backend/tests/integration/api/test_readiness.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/api/health.py backend/src/bidscope/main.py backend/src/bidscope/api/dependencies.py backend/tests/unit/test_readiness.py backend/tests/integration/api/test_readiness.py
git commit -m "feat: add dependency readiness checks"
```

### Task 5: Configure S3 timeouts and enforce concurrent-run limits

**Files:** `backend/src/bidscope/delivery/objects.py`, `backend/src/bidscope/api/dependencies.py`, `backend/src/bidscope/api/routes/runs.py`, `backend/tests/unit/delivery/test_objects.py`, `backend/tests/integration/test_run_capacity.py`

- [ ] **Step 1: Write failing object-store and capacity tests.** Assert `S3ObjectStore` builds boto3 with a botocore `Config` containing the configured connect/read timeout and max attempts. Create a `RunService` with `max_concurrent_runs=1`, start one blocking run, assert the second request returns the bounded capacity response, release the first, and assert a later run succeeds.

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_run_capacity.py -q`

Expected: FAIL because the client config and semaphore do not exist.

- [ ] **Step 3: Implement S3 client configuration.** Import `botocore.config.Config` inside the S3 construction path and pass `connect_timeout`, `read_timeout`, and `retries={"max_attempts": settings.s3_max_attempts, "mode": "standard"}` through `create_object_store`. Keep injected test clients unchanged.

- [ ] **Step 4: Implement the run semaphore.** Create an `asyncio.Semaphore(settings.max_concurrent_runs)` in `RunService` plus an integer reservation counter guarded by an `asyncio.Lock`. `schedule_run` must call a non-blocking `try_reserve_run()` before creating the task; if it returns false, raise `RunCapacityError` with code `run_capacity_exhausted`. Release the reservation in the task's `finally`, and use the same reservation path for confirm/retry resumes. Do not hold the reservation while a run is awaiting user confirmation; release it when the graph pauses and reacquire on resume. Ensure cancellation and ownership-loss paths release it exactly once.

- [ ] **Step 5: Map capacity errors.** Update `POST /api/runs` to catch `RunCapacityError` from `service.schedule_run`, return HTTP 429 with `Retry-After: 5`, and leave the newly created pending row as `retryable` with error code `run_capacity_exhausted` through a token-fenced status update. For confirm/retry, return the same 429 without changing the awaiting/retryable source state. Do not create phantom completed rows.

- [ ] **Step 6: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_run_capacity.py backend/tests/integration/api/test_runs.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/delivery/objects.py backend/src/bidscope/api/dependencies.py backend/src/bidscope/api/routes/runs.py backend/tests/unit/delivery/test_objects.py backend/tests/integration/test_run_capacity.py backend/tests/integration/api/test_runs.py
git commit -m "feat: bound object-store and run concurrency"
```

### Task 6: Add graceful shutdown and scheduler health signals

**Files:** `backend/src/bidscope/main.py`, `backend/src/bidscope/api/dependencies.py`, `backend/src/bidscope/subscriptions/scheduler.py`, `backend/src/bidscope/api/routes/events.py`, `backend/tests/integration/api/test_runtime_recovery.py`, `backend/tests/integration/test_scheduler_lock.py`, `backend/tests/unit/subscriptions/test_scheduler.py`

- [ ] **Step 1: Write failing shutdown/tick tests.** Assert `RunService.shutdown()` stops accepting new runs, cancels/drains detached tasks, and leaves retryable state persisted. Assert a scheduler tick exceeding `scheduler_tick_timeout_seconds` increments the failure metric and emits a bounded warning; advisory lock release still occurs.

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `uv run pytest backend/tests/integration/api/test_runtime_recovery.py backend/tests/integration/test_scheduler_lock.py backend/tests/unit/subscriptions/test_scheduler.py -q`

Expected: FAIL for the new scheduler timeout and lifecycle assertions.

- [ ] **Step 3: Implement shutdown ordering.** Add a `shutting_down` state shared by the app and scheduler. On lifespan exit, stop scheduler intake, set the service flag, wait up to `graceful_shutdown_seconds`, cancel remaining tasks, and then close checkpoint/database/object-store resources. Preserve the existing token-fenced cancellation repair path.

- [ ] **Step 4: Add scheduler last-success state.** Store a process-local timestamp and counters, wrap each tick in `asyncio.wait_for`, log `tick_timeout`/`tick_failed` with subscription IDs only when bounded, and expose the state to readiness/diagnostic code without inventing a scheduler HTTP endpoint.

- [ ] **Step 5: Add SSE connection limits and lifecycle logging.** Use an application-level counter/guard in the events route, reject new streams with HTTP 429 when `max_sse_connections` is reached, and always decrement the counter in `finally` on client disconnect, terminal event, or shutdown.

- [ ] **Step 6: Run tests and commit.**

Run: `uv run pytest backend/tests/integration/api/test_runtime_recovery.py backend/tests/integration/test_scheduler_lock.py backend/tests/unit/subscriptions/test_scheduler.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/main.py backend/src/bidscope/api/dependencies.py backend/src/bidscope/subscriptions/scheduler.py backend/src/bidscope/api/routes/events.py backend/tests/integration/api/test_runtime_recovery.py backend/tests/integration/test_scheduler_lock.py backend/tests/unit/subscriptions/test_scheduler.py
git commit -m "feat: make shutdown and scheduler lifecycle observable"
```

### Task 7: Update container healthchecks, CI, and operational documentation

**Files:** `Dockerfile`, `compose.yaml`, `.github/workflows/ci.yml`, `README.md`, `docs/deployment.md`, `e2e/playwright.config.ts`, `e2e/specs/readiness-and-auth.spec.ts`

- [ ] **Step 1: Add the failing E2E specification.** Create a Playwright spec that requests `/readyz`, verifies the bounded status shape, calls an API endpoint without the token and expects 401 in a production-mode test server, then repeats with the token and expects the API response. Keep existing test-mode E2E unchanged.

- [ ] **Step 2: Update container probes and stop behavior.** Change Dockerfile and Compose API healthchecks to `/readyz`; set a stop grace period at least as large as `graceful_shutdown_seconds`; keep scheduler as a separate one-instance service and add an explicit comment/runbook check for its last successful tick.

- [ ] **Step 3: Add runtime CI gates.** Extend the backend job with readiness, observability, capacity, and recovery tests. Extend Docker smoke to pass explicit production-safe settings and poll `/readyz` with the token where needed. Do not weaken the existing test-only route behavior.

- [ ] **Step 4: Document operational signals.** Record JSON log fields, `/healthz` vs `/readyz`, `/metrics` access, bounded limits, scheduler diagnosis, 429 retry behavior, and graceful stop commands in README/deployment docs.

- [ ] **Step 5: Run the complete P1-B gate.**

Run: `uv run ruff check backend scripts && uv run mypy backend/src/bidscope && uv run pytest backend/tests/unit backend/tests/contract backend/tests/security backend/tests/integration -q`

Run: `npm --prefix web run test:unit -- --run && npm --prefix web run build`

Run: `docker compose config`

Expected: all commands exit 0; Docker Compose config renders without missing variables.

- [ ] **Step 6: Commit.**

```bash
git add Dockerfile compose.yaml .github/workflows/ci.yml README.md docs/deployment.md e2e
 git commit -m "feat: add P1-B readiness and runtime gates"
```

**P1-B gate:** request IDs and bounded JSON logs correlate API/SSE/run activity, `/readyz` distinguishes dependency failure from process liveness, `/metrics` is protected and bounded, runtime limits are enforced, shutdown/recovery is tested, and Docker/CI use readiness correctly.
