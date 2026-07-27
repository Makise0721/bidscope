# BidScope P1-A Security and Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the single-tenant production deployment fail closed, protect every business endpoint with the existing Admin Token contract, add a bounded audit trail, and give the SPA a safe per-tab token flow.

**Architecture:** Keep `X-Admin-Token` as the only application credential. Centralize production validation in `Settings`, apply authentication at router boundaries, and persist security-sensitive business changes in a new `audit_events` table. The SPA stores the operator-entered token only in `sessionStorage` and adds it to same-origin requests; SSE uses an authenticated fetch stream because browser `EventSource` cannot set custom headers.

**Tech Stack:** Python 3.12, Pydantic Settings, FastAPI dependencies/middleware, SQLAlchemy async, Alembic, PostgreSQL JSONB, React 19, TypeScript, Vitest, pytest.

---

## File Map

**Create**

- `migrations/versions/f1a2b3c4d5e6_p1_audit_events.py` — append the audit table migration after current head `e7f8a9b0c1d2`.
- `backend/src/bidscope/audit.py` — bounded audit event types, redaction, and persistence helper.
- `backend/tests/unit/test_audit.py` — pure audit payload and redaction tests.
- `backend/tests/integration/test_audit_events.py` — transaction and query behavior against PostgreSQL.
- `backend/tests/security/test_production_auth.py` — fail-closed settings and route-matrix tests.
- `web/src/auth/adminToken.ts` — sessionStorage-backed token state and request header helper.
- `web/src/features/auth/AdminTokenControl.tsx` — compact token entry/clear UI.
- `web/src/test/adminToken.test.ts` — token persistence and 401 reset tests.

**Modify**

- `backend/src/bidscope/config.py` — production-only validation and explicit security settings.
- `backend/src/bidscope/api/auth.py` — strict token validation and reusable request context.
- `backend/src/bidscope/main.py` — trusted host/CORS setup, request ID middleware, and SPA bootstrap state if needed.
- `backend/src/bidscope/persistence/models.py` — `AuditEvent` ORM model.
- `backend/src/bidscope/api/routes/runs.py` — audit create/confirm/retry operations; P1 does not add a user-facing cancel endpoint.
- `backend/src/bidscope/api/routes/subscriptions.py` — audit create/pause/resume operations.
- `backend/src/bidscope/api/routes/reports.py` — audit report and DOCX reads/retries without recording content.
- `backend/src/bidscope/api/routes/sources.py` — audit source inspection/import-related administrative reads.
- `backend/src/bidscope/api/routes/evaluations.py` — audit protected evaluation reads with dataset version and evaluation ID only.
- `backend/src/bidscope/api/routes/inbox.py` — audit protected inbox reads and read-state updates with event ID/type only.
- `backend/src/bidscope/subscriptions/service.py` — propagate audit context for scheduler-triggered changes where no HTTP request exists.
- `backend/src/bidscope/snapshots/importer.py` — record successful/failed snapshot imports through the audit helper.
- `backend/src/bidscope/api/dependencies.py` — inject a shared audit repository/context into API services.
- `backend/src/bidscope/graph/executor.py` — preserve bounded error metadata when an audited mutation fails.
- `backend/tests/integration/test_subscriptions.py` — assert subscription mutation audit events.
- `backend/tests/integration/test_report_delivery.py` — assert report/DOCX audit events and redaction.
- `backend/tests/integration/test_snapshot_import.py` — assert snapshot import audit events.
- `backend/tests/integration/api/test_runs.py` — assert run lifecycle audit events.
- `backend/tests/integration/api/test_auth_and_idempotency.py` — assert authenticated mutation/idempotency behavior remains unchanged.
- `web/src/api/client.ts` — add token headers to JSON requests and replace EventSource with authenticated streaming.
- `web/src/app/App.tsx` — render the token control and keep token state outside URLs.
- `web/src/features/workbench/Workbench.tsx` — pass token-aware event stream callbacks and handle 401 reset.
- `web/src/styles/workbench.css` — style the token control without changing operational layout.
- `web/src/test/mockServer.ts` — accept and assert auth headers in frontend tests.
- `web/tests/workbench.test.tsx` — cover token-required happy path and unauthorized reset.
- `backend/tests/unit/test_cli.py` — extend Settings validation tests.
- `backend/tests/unit/test_health.py` — verify public health routes remain public while business routes require auth.
- `backend/tests/integration/conftest.py` — include `audit_events` in isolation cleanup.
- `.env.example` — document non-secret settings and explicitly mark production credentials as required.
- `.env.production.example` — add a safe production template with no real secrets.
- `README.md` and `docs/deployment.md` — document the single-tenant token flow and production startup checks.

---

### Task 1: Lock the production configuration contract

**Files:** `backend/tests/unit/test_cli.py`, `backend/src/bidscope/config.py`, `.env.example`, `.env.production.example`

- [ ] **Step 1: Write failing settings tests.** Add tests for `Settings(app_mode="production")` rejecting a missing `admin_token`, a token shorter than the configured minimum, a known placeholder such as `change-me`, `object_store_type="local"`, and wildcard CORS. Add a passing case with a strong token, explicit S3 fields, and an explicit allowed origin.

```python
def test_production_settings_fail_closed() -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        Settings(app_mode="production")

    with pytest.raises(ValidationError, match="placeholder"):
        Settings(app_mode="production", admin_token="change-me")


def test_production_settings_accept_explicit_storage_and_origin() -> None:
    settings = Settings(
        app_mode="production",
        admin_token="a" * 32,
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope-prod",
        s3_access_key="access",
        s3_secret_key="secret",
        allowed_origins=["https://bidscope.example.test"],
    )
    assert settings.allowed_origins == ["https://bidscope.example.test"]
```

- [ ] **Step 2: Run the focused tests and verify they fail.**

Run: `uv run pytest backend/tests/unit/test_cli.py -q`

Expected: FAIL because the new production-only fields and validators do not yet exist.

- [ ] **Step 3: Implement the minimal settings contract.** Add typed settings for `admin_token_min_length`, `allowed_origins`, `trusted_hosts`, `external_scheme`, `s3_region`, and `production_placeholder_tokens`. Add an after-validator that runs only for `app_mode == "production"`; it must require a non-placeholder admin token, explicit S3 configuration, and a non-wildcard origin list. Keep demo/test defaults unchanged. Never log the secret values.

- [ ] **Step 4: Add safe environment templates.** Keep `.env.example` usable for demo/test and create `.env.production.example` with variable names, comments, and empty secret values. Do not add actual credentials or MinIO defaults to the production template.

- [ ] **Step 5: Run the focused tests and static checks.**

Run: `uv run pytest backend/tests/unit/test_cli.py -q`

Expected: PASS with the existing CLI tests plus the new production settings cases.

Run: `uv run ruff check backend/src/bidscope/config.py backend/tests/unit/test_cli.py`

Expected: exit 0.

- [ ] **Step 6: Commit.**

```bash
git add backend/src/bidscope/config.py backend/tests/unit/test_cli.py .env.example .env.production.example
git commit -m "feat: enforce production configuration baseline"
```

### Task 2: Harden the Admin Token dependency and route matrix

**Files:** `backend/tests/security/test_production_auth.py`, `backend/src/bidscope/api/auth.py`, all `backend/src/bidscope/api/routes/*.py`, `backend/src/bidscope/main.py`, `backend/tests/unit/test_health.py`

- [ ] **Step 1: Write the failing auth matrix test.** Build `create_app(Settings(app_mode="production", admin_token="a" * 32, ...))` without entering its lifespan and assert `/healthz` is 200, an unprotected `/api/runs` request is 401, a wrong token is 401, and the correct `X-Admin-Token` reaches the router dependency. Add a test that the test-control router is absent outside test mode.

- [ ] **Step 2: Run the focused security tests.**

Run: `uv run pytest backend/tests/security/test_production_auth.py backend/tests/unit/test_health.py -q`

Expected: FAIL for any route missing the dependency or any new production validation field.

- [ ] **Step 3: Implement strict token validation.** Keep `require_admin_token` as the shared dependency. Compare the supplied header to the configured secret using `secrets.compare_digest`, reject missing/empty/oversized values with 401, and never include the supplied or expected value in an exception. Preserve demo/test bypass only through `app_mode`.

- [ ] **Step 4: Add explicit app middleware.** Configure `TrustedHostMiddleware` and `CORSMiddleware` from settings in `create_app`. Use an explicit allowed-origin list and `allow_credentials=False`; do not enable wildcard origins in production. Keep `/healthz` and `/readyz` outside business router dependencies.

- [ ] **Step 5: Verify every router boundary.** Confirm `runs`, `events`, `reports`, `subscriptions`, `inbox`, `sources`, and `evaluations` all declare `Depends(require_admin_token)` at router level. Add a test that walks `app.routes` and checks the expected protected path prefixes are not exposed without the dependency. Keep `/api/test-controls/*` registration conditional on `app_mode == "test"` and its token independent.

- [ ] **Step 6: Run security tests and commit.**

Run: `uv run pytest backend/tests/security backend/tests/unit/test_health.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/api/auth.py backend/src/bidscope/main.py backend/src/bidscope/api/routes backend/tests/security/test_production_auth.py backend/tests/unit/test_health.py
git commit -m "feat: enforce single-tenant API authentication"
```

### Task 3: Add the audit model, migration, and redacted event writer

**Files:** `backend/src/bidscope/persistence/models.py`, `migrations/versions/f1a2b3c4d5e6_p1_audit_events.py`, `backend/src/bidscope/audit.py`, `backend/tests/unit/test_audit.py`, `backend/tests/integration/test_audit_events.py`, `backend/tests/integration/conftest.py`

- [ ] **Step 1: Write pure redaction tests.** Test that a payload containing `X-Admin-Token`, `Authorization`, `Cookie`, `BIDSCOPE_MODEL_API_KEY`, and nested secret-like keys is replaced with `"[REDACTED]"`; IDs and bounded error codes remain. Test that oversized message/details are truncated deterministically.

- [ ] **Step 2: Run the unit tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_audit.py -q`

Expected: FAIL because `bidscope.audit` does not exist.

- [ ] **Step 3: Add the ORM model and migration.** Add `AuditEvent` with UUID primary key, `occurred_at`, `event_type`, `outcome`, nullable request/run/subscription/report/import IDs stored as UUID/text-compatible fields, normalized method/path, error code, and JSONB `details`. Use revision `f1a2b3c4d5e6` with `down_revision = "e7f8a9b0c1d2"`; do not alter old migration files. Add indexes on `occurred_at`, `event_type`, and the major correlation IDs.

- [ ] **Step 4: Implement bounded audit helpers.** Define `AuditContext`, `AuditEventType`, `redact_audit_value`, and `record_audit_event(session, context, event_type, outcome, details)`. Use an allowlist of fields, maximum string lengths, maximum nested depth, and no raw headers. The helper must not accept token/API key values as a normal field.

- [ ] **Step 5: Add integration coverage.** Create an event in a transaction, commit, query it by `request_id` and `event_type`, and assert the redacted JSON. Add a rollback test proving a transaction-scoped audit event disappears when its business transaction rolls back. Extend the existing truncation cleanup with `TRUNCATE TABLE audit_events`.

- [ ] **Step 6: Run migration and tests.**

Run: `BIDSCOPE_APP_MODE=test BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test uv run alembic upgrade head`

Expected: migration exits 0 and creates `audit_events`.

Run: `uv run pytest backend/tests/unit/test_audit.py backend/tests/integration/test_audit_events.py -q`

Expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add backend/src/bidscope/persistence/models.py backend/src/bidscope/audit.py migrations/versions/f1a2b3c4d5e6_p1_audit_events.py backend/tests/unit/test_audit.py backend/tests/integration/test_audit_events.py backend/tests/integration/conftest.py
git commit -m "feat: add redacted audit event persistence"
```

### Task 4: Instrument critical mutations without breaking existing transactions

**Files:** `backend/src/bidscope/api/dependencies.py`, `backend/src/bidscope/api/routes/runs.py`, `backend/src/bidscope/api/routes/subscriptions.py`, `backend/src/bidscope/api/routes/reports.py`, `backend/src/bidscope/api/routes/sources.py`, `backend/src/bidscope/api/routes/evaluations.py`, `backend/src/bidscope/api/routes/inbox.py`, `backend/src/bidscope/subscriptions/service.py`, `backend/src/bidscope/snapshots/importer.py`, `backend/tests/integration/api/test_runs.py`, `backend/tests/integration/test_subscriptions.py`, `backend/tests/integration/test_report_delivery.py`, `backend/tests/integration/test_snapshot_import.py`

- [ ] **Step 1: Add failing transaction tests.** Cover run confirmation, retry, subscription create/pause/resume, snapshot import, and DOCX retry. For each critical mutation, assert one audit row exists after success and no audit row exists after an intentional business transaction rollback. Assert the audit row contains IDs/status only, never request text or report body.

- [ ] **Step 2: Implement one transaction-aware audit boundary.** Pass an `AuditContext` from request middleware/route dependencies into the existing session or service transaction. For HTTP mutation routes, write the audit row on the same `AsyncSession` before commit. For scheduler/import code without an HTTP request, create a context with `request_id=None` and the relevant subscription/import ID. Do not create a second independent commit for critical mutations.

- [ ] **Step 3: Instrument run and subscription lifecycle.** Add events for run created, confirmed, retried, subscription created, paused, resumed, tick started, tick completed, and tick failed. P1 does not add a user-facing cancellation endpoint; existing internal task cancellation remains covered by P0 recovery tests and P1-B shutdown observability. Ensure idempotent duplicate requests do not create duplicate business-side audit events unless the attempt itself is explicitly an allowed observation event.

- [ ] **Step 4: Instrument snapshot and report operations.** Record snapshot import success/failure and report/DOCX retry/download with object key and report/run IDs only. Never record object bytes, report claims, request body, or credentials.

- [ ] **Step 5: Run the focused regression suites.**

Run: `uv run pytest backend/tests/integration/test_audit_events.py backend/tests/integration/test_idempotency.py backend/tests/integration/test_report_delivery.py backend/tests/integration/test_subscriptions.py -q`

Expected: PASS with existing idempotency and delivery behavior unchanged.

- [ ] **Step 6: Commit.**

```bash
git add backend/src/bidscope/api/dependencies.py backend/src/bidscope/api/routes backend/src/bidscope/subscriptions/service.py backend/src/bidscope/snapshots/importer.py backend/tests
 git commit -m "feat: audit critical BidScope operations"
```

### Task 5: Add SPA Admin Token storage and authenticated streaming

**Files:** `web/src/auth/adminToken.ts`, `web/src/features/auth/AdminTokenControl.tsx`, `web/src/api/client.ts`, `web/src/app/App.tsx`, `web/src/features/workbench/Workbench.tsx`, `web/src/styles/workbench.css`, `web/src/test/adminToken.test.ts`, `web/tests/workbench.test.tsx`, `web/src/test/mockServer.ts`

- [ ] **Step 1: Write failing token helper tests.** Mock `sessionStorage`; assert `getAdminToken`, `setAdminToken`, and `clearAdminToken` never touch `localStorage` and return null after clear. Assert a 401 response invokes the registered unauthorized callback and clears storage.

- [ ] **Step 2: Run the frontend unit tests and verify failure.**

Run: `npm --prefix web run test:unit -- --run web/src/test/adminToken.test.ts`

Expected: FAIL because the auth module does not exist.

- [ ] **Step 3: Implement token state.** Create a small module with `ADMIN_TOKEN_STORAGE_KEY`, `getAdminToken`, `setAdminToken`, `clearAdminToken`, `onUnauthorized`, and a `buildAuthHeaders` helper. Keep the token in `sessionStorage` only; reject empty values before storing.

- [ ] **Step 4: Centralize JSON request headers.** Update `requestJson`, `createRun`, `confirmRun`, `retryRun`, report, subscription, source, inbox, evaluation, and run-history calls to merge `Content-Type` and `X-Admin-Token`. On 401, clear the token and surface a stable `UnauthorizedError` for the UI.

- [ ] **Step 5: Replace EventSource with an authenticated fetch stream.** Change `streamRunEvents` to call `fetch` with `Accept: text/event-stream`, `X-Admin-Token`, and an `AbortController`. Parse SSE frames from `ReadableStream<Uint8Array>` by blank-line boundaries, dispatch the existing bounded event names, support `after_seq`, and close on `terminal`. Return the unsubscribe function that aborts the controller. Keep malformed frames best-effort ignored, matching the current behavior.

- [ ] **Step 6: Add the token control and 401 UX.** Render `AdminTokenControl` in `App`; provide input, save, clear, and status text. Keep the token out of the URL and React error messages. Make `Workbench` stop the stream and return to an auth-needed state on `UnauthorizedError`.

- [ ] **Step 7: Update tests and run build.** Extend MSW handlers to assert/accept the header, cover a run + stream with the token, and cover a 401 clearing the token. Run:

```bash
npm --prefix web run test:unit -- --run
npm --prefix web run build
```

Expected: all frontend tests pass and TypeScript/Vite build exits 0.

- [ ] **Step 8: Commit.**

```bash
git add web/src web/tests
 git commit -m "feat: add secure single-tenant frontend authentication"
```

### Task 6: Complete P1-A documentation and gate

**Files:** `README.md`, `docs/deployment.md`, `.env.production.example`, `.github/workflows/ci.yml`

- [ ] **Step 1: Document production startup.** Add exact steps for generating an Admin Token, setting production env/secrets, configuring the reverse proxy, loading the SPA token in the current tab, and rotating the token. State that `/healthz` is public, `/readyz` is operational, `/api/*` is protected, and test controls are test-only.

- [ ] **Step 2: Add P1-A CI gate.** Add a job or extend the security job to run the production settings tests, route matrix, audit unit/integration tests, and web auth unit tests. Ensure the migration is applied in the integration job before audit tests.

- [ ] **Step 3: Run the complete P1-A gate.**

Run: `uv run ruff check backend scripts && uv run mypy backend/src/bidscope && uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q`

Run: `npm --prefix web run test:unit -- --run && npm --prefix web run build`

Expected: exit 0 for every command.

- [ ] **Step 4: Commit the gate and docs.**

```bash
git add README.md docs/deployment.md .env.production.example .github/workflows/ci.yml
git commit -m "docs: document P1-A production security baseline"
```

**P1-A gate:** production settings fail closed, all protected routes reject unauthenticated requests, critical mutations have redacted audit rows, SPA requests and SSE carry the token, and all backend/frontend checks pass.
