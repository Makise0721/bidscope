# BidScope P0 Remediation Design

**Status:** Approved for implementation  
**Date:** 2026-07-23  
**Scope:** P0 acceptance blockers and high-value P1 hardening found in the final P0 review

## 1. Goal

Restore one deployable, evidence-first P0 execution path:

```text
validated snapshot -> durable query run -> evidence-bound online report
  -> idempotent DOCX export -> confirmed subscription incremental delivery
```

The remediation preserves the existing snapshot-only source policy, bounded LangGraph workflow, PostgreSQL persistence, and no-live-fetch runtime boundary. It does not introduce queues, Redis, Celery, live source connectors, or new user-facing product scope.

## 2. Runtime Boundary

The FastAPI application owns long-lived resources in its lifespan:

- SQLAlchemy engine and session factory.
- One `AsyncPostgresSaver` context for graph checkpoints.
- A compiled graph using injected fake or configured real model ports.
- The configured local or S3-compatible object store.
- Report persistence/delivery and subscription services that use the same run execution path.

`InMemorySaver` remains allowed only for isolated graph unit tests. API, scheduler, and E2E processes must compile the graph with `AsyncPostgresSaver` using `Settings.checkpoint_database_url`.

At startup, the app marks stale `pending` and `running` runs as `retryable`, preserving checkpoints. A retry resumes the existing thread ID through the graph rather than creating a second logical query run.

## 3. Report Persistence and Delivery

### 3.1 Delivery contract

`GraphDeps` receives a typed report persistence port. The graph's `persist_and_deliver` node converts its validated `ReportDraft`, `VerifiedOpportunity` records, and `NoticeEvidence` bindings into the domain `Report` model and calls that port.

The persistence operation writes, in a single database transaction:

1. One `reports` row keyed by `run_id`.
2. Ordered `report_items` for each reported opportunity.
3. Claim-to-evidence relations, including source notice version IDs and immutable span hashes.
4. Report conditions, freshness/completeness warnings, and data-source provenance.

The transaction is idempotent by `run_id`: a replayed or resumed `persist_and_deliver` returns the existing logical report instead of inserting duplicate rows.

### 3.2 DOCX failure semantics

After the online report transaction commits, a separate idempotent DOCX export renders only the persisted typed report. The export key is deterministic from report ID and renderer version.

A DOCX failure must:

- Keep the online report available.
- Record a bounded `DeliveryError` on the run/report export state.
- Leave the report export retryable without rerunning retrieval, model, evidence, or report persistence work.

A successful repeated export reuses the existing object/record. Reports API responses include report items, claims, citations, evidence spans, source versions, capture kind, provenance, and completeness warning, all with bounded DTO fields.

## 4. Confirmed Runs and Subscriptions

### 4.1 API run lifecycle

`POST /api/runs` accepts an `Idempotency-Key` header. The service derives a deterministic run key from that header and request scope, returning the existing run on a replay. A run is persisted before background execution starts; startup recovery converts unstarted `pending` rows to `retryable`.

The API returns the actual persisted status. The workbench does not assume every query requires approval. `confirm` remains valid only for `awaiting_confirmation` runs.

### 4.2 Subscription lifecycle

A subscription can only be created from a completed, explicitly confirmed run that contains a schedule. The server stores that run's normalized intent rather than accepting an arbitrary intent dictionary from the public route.

Each scheduled trigger uses the same graph and report persistence path as an interactive run. It obtains the persisted report before calculating delivery deltas. Only after the report transaction commits may the subscription transaction:

1. Compare current source notice versions with its seen set.
2. Create idempotent `new_notice` or `material_change` inbox events.
3. Advance `subscription_seen_items`.
4. Mark the trigger successful and schedule the next execution.

Material-change checks reuse the defined business fields: deadline, budget, region, purchaser, scope, cancellation state, and claim-supporting source text. A content-hash-only formatting change is not a material change.

The PostgreSQL advisory lock remains keyed by subscription and time bucket. Repeated failures retain the existing pause-and-inbox behavior.

## 5. Object Storage and Deployment

`Settings` contains explicit object-store discriminated configuration:

- `object_store_type: local | s3`
- `object_store_root`
- `s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, and optional prefix

The application builds exactly one object-store implementation from those settings. S3-compatible stores use the configured endpoint and credentials; local stores create a writable root during application setup. MinIO bootstrap creates the configured bucket before API/scheduler depend on it.

The production image contains:

- `alembic.ini`, `migrations/`, and database initialization assets needed by documented commands.
- The built SPA and source fixture data.
- A writable root owned by UID 1000 for local data when local storage is selected.

All process entry points use canonical commands:

```text
bidscope api serve --host 0.0.0.0 --port 8000
bidscope scheduler start
```

Alembic, CI, Docker Compose, docs, and E2E use `postgresql+psycopg://...` for synchronous migrations/checkpoints. No path may silently fall back to `psycopg2`.

## 6. Security and Input Boundaries

Production API operations require the configured `X-Admin-Token` except `/healthz`; test-control routes remain registered only in test mode and require their independent `X-Test-Control-Token`.

Snapshot provenance validation rejects:

- URLs with userinfo.
- Ports other than the default HTTPS port.
- Non-HTTPS URLs and unapproved exact hosts.

The runtime remains snapshot-only. No change adds a public-site HTTP fetch operation.

## 7. Workbench and E2E Behavior

The workbench has explicit states for pending/running, awaiting confirmation, completed report, retryable failure, and partial-source warning. It:

- Polls or consumes SSE events with `Last-Event-ID` resumption.
- Shows ordered node events and terminal state.
- Renders parsed intent from API data, with correction controls before approval.
- Retrieves the persisted report only after completion.
- Presents bounded evidence/citation details and source-version provenance.
- Keeps `synthetic_demo` records visibly labeled, and presents `example.invalid` URLs as plain text.
- Provides a narrow-screen evidence/trace drawer.

Playwright provisions an isolated test environment before browser flows: migrations, checkpoint setup, Batch 1 import, correct API command, and a generated test-control token. It exercises all six P0 flows with non-conditional assertions. Desktop and mobile projects assert the corresponding UI state and retain screenshot artifacts on failure.

## 8. Verification Gate

The remediation is complete only when all of the following have fresh passing evidence:

1. Unit, contract, security, and integration suites run with the documented test-mode settings.
2. A real completed API run over Batch 1 produces a persisted report with valid citation/evidence bindings and an idempotent DOCX download.
3. A process restart resumes confirmation/retry work from PostgreSQL checkpoints and does not duplicate upstream node events.
4. Subscription execution runs the real report path, emits only new/material changes after Batch 2, and advances its seen cursor only after report commit.
5. Docker Compose starts API and scheduler with the canonical commands; container migrations and checkpoint setup run successfully; configured MinIO storage is used.
6. CI executes database integration, security tests, image startup smoke, Web build/tests, and all Playwright flows.
7. Security regression tests reject URL userinfo/non-default ports and API idempotency/authentication tests prove enforced boundaries.

## 9. Non-Goals

This remediation does not:

- Introduce live source fetching, web scraping, CAPTCHA handling, or arbitrary URL access.
- Add multi-user tenancy, billing, user identity management, or external notification delivery.
- Convert fixture-consistency evaluation metrics into live-performance claims.
- Refactor unrelated ingestion, retrieval, or model behavior outside the integration points required above.
