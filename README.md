# BidScope

**Evidence-first tender intelligence Agent.**

BidScope parses public tender notices from Chinese government procurement sources, deduplicates them, verifies evidence, and generates cited reports. It is designed so that every factual claim in every report resolves to an immutable evidence span and source version.

**Status:** P1 security, observability, readiness, bounded runtime, backup/restore, and a clean-host recovery drill are implemented. External backup replication remains disabled unless its explicit, separate credentials are supplied. BidScope remains snapshot-only with deterministic offline evaluation.

---

## What BidScope Does

1. **Ingests** verified snapshot bundles from official sources through a shared adapter contract. Snapshots are versioned, hash-validated, and never overwritten.
2. **Normalizes** notices into a common schema with strict provenance tracking (source, URL, capture time, content hash).
3. **Deduplicates** notices using deterministic rules and optional semantic matching.
4. **Retrieves** notices via hybrid search (keyword + vector) over the committed corpus.
5. **Generates** cited reports where every factual claim links back to an immutable evidence span.
6. **Schedules** recurring queries and surfaces only new or materially changed notices.

Official sources: [中国政府采购网 (CCGP)](https://www.ccgp.gov.cn/) and [全国公共资源交易平台 (GGZY)](https://www.ggzy.gov.cn/).

### Production Security Baseline (P1-A)

The production Compose profile is a single-tenant administrator deployment. It fails closed during settings construction and does not silently fall back to the demo configuration:

- `BIDSCOPE_APP_MODE=production` is required.
- `BIDSCOPE_ADMIN_TOKEN` must be non-empty, at least `BIDSCOPE_ADMIN_TOKEN_MIN_LENGTH` bytes after UTF-8 encoding, and different from example or placeholder values. Production startup fails if it is missing, too short, or a placeholder.
- Production uses `BIDSCOPE_OBJECT_STORE_TYPE=s3` with explicit endpoint, region, bucket, prefix, access key, and secret key. Ambient credentials and the local object store are not accepted for production.
- `BIDSCOPE_REAL_MODEL_ENABLED=true` requires `BIDSCOPE_MODEL_API_KEY`. The key is never written to logs, run events, or audit details.
- `BIDSCOPE_ALLOWED_ORIGINS`, `BIDSCOPE_TRUSTED_HOSTS`, and `BIDSCOPE_EXTERNAL_SCHEME=https` are explicit transport settings. Production origins must be exact browser origins; wildcard origins and wildcard trusted hosts are rejected.
- PostgreSQL application and checkpoint DSNs must use their explicit supported drivers, credentials, authority, database name, and allowlisted TLS query only. Demo DSNs, ambient credentials, target overrides, fragments, and unknown query parameters are rejected.

The endpoint policy is:

| Endpoint area | Access |
|---|---|
| `/healthz` | Public process liveness response; no dependency details |
| `/assets/*` and SPA GET | Public, or restricted by the reverse proxy |
| `/api/runs/*`, `/api/reports/*`, `/api/subscriptions/*`, `/api/inbox-events`, `/api/sources/*`, `/api/evaluations/*` | `X-Admin-Token` required |
| `/api/test-controls/*` | Registered only in `app_mode=test`; requires the separate test-control token |

P1-A does not provide a user-account system, cookies, query-string credentials, or a user-visible cancellation endpoint. The API checks the Admin Token at the router boundary and returns the stable `401 {"detail":"invalid admin token"}` response for missing, wrong, empty, or oversized values. Trusted Host and explicit CORS middleware remain active in production; CORS does not enable credentials.

### Browser Admin Token Flow

Open the SPA through the configured same-origin public entry point, then enter the Admin Token in the Workbench access control. The frontend trims the value, stores it only in the current tab's `sessionStorage`, and sends it as `X-Admin-Token` on JSON requests and the authenticated SSE stream. It never puts the token in the bundle, URL, hash, `localStorage`, Cookie, log, or report. Use **Clear** to remove it from the tab. A `401` clears the stored token and returns the UI to the authentication-required state; enter the token again to continue.

To rotate the credential, generate a new random token, update `BIDSCOPE_ADMIN_TOKEN` in the deployment secret, restart both `api` and `scheduler`, and enter the new token in each open browser tab. Treat old tabs as unauthenticated until their token is replaced. Never commit the deployment `.env` file or real credentials.

The application writes bounded audit events for critical run, subscription, snapshot-import, report, and DOCX operations. Critical mutation audit rows use the same database transaction as the business change; observation audit failures do not block ordinary reads. Audit details contain IDs, status, and bounded metadata only. Admin Tokens, Authorization headers, model keys, Cookies/session data, raw request headers, request bodies, and full report bodies are excluded.

`/readyz` checks configuration, PostgreSQL, checkpoint, and object storage dependencies within bounded time and returns `200` only when all checks pass. `/healthz` remains a public process liveness probe. `GET /metrics` is Admin Token protected and returns bounded Prometheus text; request IDs are accepted from `X-Request-ID` or generated and echoed in responses. Capacity exhaustion returns `429` with `Retry-After: 5`; scheduler tick timeout and SSE lifecycle are logged/metricized without user text labels. Backup creation, verification, pruning, and non-destructive restore are explicit CLI operations. The clean-host recovery gate is `bash scripts/backup_restore_smoke.sh`: it uses only synthetic data and the fake model, then prints a JSON artifact with the backup ID, manifest hash, explicit backup age/RPO and restore-duration/RTO measurements, restored report evidence version, and `passed`. A gate passes only when `rpo_hours <= 24` and `rto_seconds <= 14400`; the CI artifact is retained as recovery evidence.

---

## Source Policy

**P0 is snapshot-only.** BidScope does not crawl, scrape, or probe any public procurement website. All search and reporting run against data that has already been imported through an explicit, auditable CLI action.

There are three evidence grades:

| Grade | Meaning | URL constraint |
|---|---|---|
| `raw_response` | Real original response, obtained through a separately authorized process | Official HTTPS host whitelist |
| `curated_public_excerpt` | Human-verified excerpt of public fields | Official HTTPS host whitelist |
| `synthetic_demo` | Clearly synthetic data for demos, regression, E2E | `https://example.invalid/` only |

Full policy: [`docs/source-policy.md`](docs/source-policy.md).

---

## Architecture

```
                   +---------------------------+
                   |     React SPA (Vite)      |
                   |   served at :8000/static  |
                   +---------------------------+
                              |
                              | HTTP / SSE
                              v
+------------------+    +---------------------------+
|   MinIO / S3     |<---|     FastAPI Backend       |
| (DOCX, payloads) |    |     (bidscope api)        |
+------------------+    +---------------------------+
                              |
                              | SQLAlchemy / pgvector
                              v
                        +-----------+
                        | PostgreSQL|
                        | (pgvector)|
                        +-----------+

+------------------+
|   APScheduler    |--- runs as a separate process role (bidscope scheduler start)
| (subscriptions)  |--- PostgreSQL advisory locks guarantee single execution
+------------------+
```

A single Docker image runs both process roles (`api` and `scheduler`), selected by command. PostgreSQL and MinIO are external dependencies.

- **Backend:** Python 3.12, FastAPI, LangGraph, SQLAlchemy (async), Alembic, APScheduler.
- **Frontend:** React 19, TypeScript, Vite, TanStack Query.
- **Database:** PostgreSQL 17 with pgvector.
- **Object storage:** MinIO (S3-compatible) for production; local filesystem for `demo`/`development`.
- **LLM:** DeepSeek (live, opt-in) or deterministic fake model (default, P0).

---

## Setup

### Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL 17 with pgvector (or use Docker Compose)
- Node.js 22+ (for the web frontend)

### Install

```bash
# Install Python dependencies
uv sync

# Copy environment template
cp .env.example .env
# Edit .env to set BIDSCOPE_DATABASE_URL and other variables
```

### Database

```bash
# Start infrastructure (PostgreSQL + MinIO)
docker compose up -d postgres minio

# Apply migrations
alembic upgrade head

# Create LangGraph checkpoint tables
uv run bidscope checkpoints setup
```

### Initial Data

```bash
# Import a snapshot bundle (after obtaining one through an authorized process)
uv run bidscope snapshots inspect path/to/bundle
uv run bidscope snapshots import path/to/bundle
```

The repository ships with synthetic demo data for the evaluation system (`source=synthetic_demo`, `example.invalid` URLs). No real tender notices are included.

---

## Demo Flow

With the infrastructure running and migrations applied:

```bash
# Start the API (serves both the backend and the built SPA)
uv run bidscope api serve
# Or, if the frontend is not built:
npm --prefix web run build
uv run bidscope api serve
```

Then open `http://localhost:8000`:

1. Enter a natural-language query (e.g., "云南教育采购 2026年7月").
2. Review the confirmed intent (topics, regions, budget range, schedule).
3. Inspect retrieved notices and their provenance.
4. Generate and download a cited DOCX report.
5. Create a recurring subscription.

The default `demo` mode uses a deterministic fake model and the local object store — no API key or network access required.

---

## Tests

```bash
# Unit tests + contract tests (no database required)
uv run pytest backend/tests/unit backend/tests/contract -q

# Integration tests (requires PostgreSQL with pgvector)
export BIDSCOPE_DATABASE_URL="postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
uv run pytest backend/tests/integration -q

# Security tests
uv run pytest backend/tests/security -q

# Web frontend tests
npm --prefix web run test:unit

# Deterministic evaluation (no database required)
uv run --offline bidscope eval run --mode deterministic --output eval/results/deterministic.json
```

Restricted real-data staging uses a separate metadata-only acceptance command;
it validates an access-controlled dataset manifest and result artifact without
running a model or changing the deterministic `target_pass` gate:

```bash
uv run bidscope eval validate-real \
  --manifest /controlled/staging/evaluation/dataset-manifest.json \
  --catalog /controlled/staging/evaluation/snapshot-admission-catalog.json \
  --catalog-signature /controlled/staging/evaluation/snapshot-admission-catalog.sig \
  --result /controlled/staging/evaluation/result.json \
  --json
```

This command requires `BIDSCOPE_REAL_EVALUATION_CATALOG_PUBLIC_KEY`, a
Base64-encoded Ed25519 public key. The catalog must carry a detached signature
from the corresponding external private key; an unsigned or altered catalog is
blocked. The private key must never be configured in BidScope.

Real data, prompts, credentials and evaluation payloads are not committed to
the repository. See the productization governance and evaluation specifications
under `docs/superpowers/specs/` before creating a staging batch.

### Test Structure

| Suite | Path | Requirements |
|---|---|---|
| Unit | `backend/tests/unit/` | None |
| Contract | `backend/tests/contract/` | None |
| Integration | `backend/tests/integration/` | PostgreSQL with pgvector |
| Security | `backend/tests/security/` | Varies |

---

## Evaluation

BidScope ships a **fully offline, deterministic evaluation runner** that scores the pipeline against committed JSONL fixtures. No network, no live model, no real tender data.

### Methodology

The runner (`bidscope eval run`) executes intent parsing, retrieval, deduplication, citation verification, and end-to-end scenarios against six fixed datasets and scores them with pure functions. Every run records git commit, working tree dirty flag, dataset hashes, and elapsed time for provenance.

### Datasets

| Dataset | Count | What it scores |
|---|---|---|
| `corpus` | 120 | Retrieval corpus |
| `intent-v1` | 120 | Field exact match, macro F1, error accuracy |
| `retrieval-v1` | 30 | Recall@10, nDCG@10 |
| `dedup-v1` | 120 | Precision, recall, F1 |
| `claims-v1` | 60 | Citation coverage, correctness, support accuracy |
| `e2e-v1` | 30 | Task success, P50/P95 latency, tokens, cost |

All datasets use `source=synthetic_demo` and `example.invalid` URLs. They are **not** real tender notices.

### Targets vs Measured

> **Design principle (section 16, criterion 9):** Targets are stated separately from measured values. Measured results must be reported alongside the dataset version, model name, pricing snapshot date, and environment.

| Metric | Target | Mode |
|---|---|---|
| Intent Macro F1 | >= 90% | `fixture_consistency` |
| Retrieval Recall@10 | >= 85% | `fixture_consistency` |
| Retrieval nDCG@10 | >= 85% | `fixture_consistency` |
| Dedup F1 | >= 90% | `fixture_consistency` |
| Citation coverage | 100% | `fixture_consistency` |
| Citation correctness | >= 95% | `fixture_consistency` |
| Task success rate | >= 95% | `fixture_consistency` |
| P95 latency | <= 15,000 ms | `fixture_consistency` |
| Cost | <= CNY 0.10 | `fixture_consistency` |

### Current Measured Result

- **Provider:** `offline` — **Model:** `fake-deterministic`
- **Pricing snapshot:** `2026-07-18` (CNY 0.00 / million tokens)
- **Network:** disabled — **App mode:** demo
- **`target_pass`: true**
- Dataset counts: corpus 120, intent-v1 120, retrieval-v1 30, dedup-v1 120, claims-v1 60, e2e-v1 30

Full details: [`docs/evaluation.md`](docs/evaluation.md).

---

## Security Boundaries

- **No live network access** in P0. The snapshot path and the scheduler never call public procurement sites.
- **Provenance enforcement.** Each notice is validated against an official-host whitelist and a manifest hash. Synthetic data can never impersonate an official source.
- **Test-only routes** (`/api/test-controls/*`) are registered only when `app_mode == "test"` and are gated by a token.
- **Non-root container execution.** The Docker image runs as UID 1000.
- **Idempotent operations.** Snapshot import, report generation, and subscription execution are guarded by idempotency keys and advisory locks.

---

## Deployment

See [`docs/deployment.md`](docs/deployment.md) for the full deployment guide.

Quick start:

```bash
docker compose up -d postgres minio
docker compose run --rm api alembic upgrade head
docker compose run --rm api bidscope checkpoints setup
docker compose up -d api
# Optional: docker compose up -d scheduler
```

The API is available at `http://localhost:8000`. Process liveness is `GET /healthz`; dependency readiness is `GET /readyz`.

---

## Limitations

- **P0 is snapshot-only.** There is no live fetching of tender notices. All data must be explicitly imported.
- **All evaluation metrics are fixture consistency.** They measure whether the deterministic pipeline reproduces its committed expected outputs, not whether the system performs well against real tender data.
- **The corpus is synthetic.** The 120-notice evaluation corpus uses `source=synthetic_demo` and `example.invalid` URLs. No real tender notices are included in the repository.
- **Zero-cost is a fixture.** The CNY 0.00 pricing is an offline snapshot, not a live cost measurement.
- **Latency is committed, not measured.** P95 latency comes from the e2e dataset's `latency_ms` field, not from live timing.
- **Live-model runs are not reproducible.** Enabling a real model provider breaks determinism and incurs real cost.
- **Playwright E2E covers the full interactive + subscription flow** against a real API and the `bidscope_e2e` database. Run it locally with Postgres up:
  ```bash
  BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e
  ```
  The webServer step builds the SPA, resets+migrates+seeds `bidscope_e2e`, and starts `bidscope api serve`. Both desktop (1440×900) and mobile (390×844) projects run (12 tests total).
- **DeepSeek is the only supported live provider.** Other OpenAI-compatible providers may work but are not tested.

---

## Project Narrative

BidScope is a portfolio project that demonstrates how to build an evidence-first AI system with defensible technical decisions:

- **Ingestion decoupled from interactive runs.** Snapshots are imported through a separate, auditable path so that interactive queries never trigger network access.
- **One bounded graph, not ornamental multi-agent orchestration.** A single LangGraph with explicit nodes, interrupts, and checkpoints keeps the system inspectable and recoverable.
- **Deterministic code where it belongs.** Deduplication, provenance validation, retrieval ranking, and citation verification are all deterministic. The LLM is reserved for intent parsing and synthesis.
- **Evidence and immutable versions prevent unsupported summaries.** Every claim resolves to an immutable evidence span; source versions are never overwritten.
- **Failure recovery without wasted work.** LangGraph checkpoints and node-level retries let a transient failure resume without repeating successful upstream model work.
- **Evaluated separately, reported honestly.** Retrieval, deduplication, citations, success rate, latency, and cost each have their own metric. Targets are stated separately from measured values.

---

## License

Private portfolio project. Not licensed for public redistribution.
