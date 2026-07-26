# BidScope

**Evidence-first tender intelligence Agent.**

BidScope parses public tender notices from Chinese government procurement sources, deduplicates them, verifies evidence, and generates cited reports. It is designed so that every factual claim in every report resolves to an immutable evidence span and source version.

**Status:** P0 snapshot-only. No live web fetching. Deterministic offline evaluation.

---

## What BidScope Does

1. **Ingests** verified snapshot bundles from official sources through a shared adapter contract. Snapshots are versioned, hash-validated, and never overwritten.
2. **Normalizes** notices into a common schema with strict provenance tracking (source, URL, capture time, content hash).
3. **Deduplicates** notices using deterministic rules and optional semantic matching.
4. **Retrieves** notices via hybrid search (keyword + vector) over the committed corpus.
5. **Generates** cited reports where every factual claim links back to an immutable evidence span.
6. **Schedules** recurring queries and surfaces only new or materially changed notices.

Official sources: [中国政府采购网 (CCGP)](https://www.ccgp.gov.cn/) and [全国公共资源交易平台 (GGZY)](https://www.ggzy.gov.cn/).

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

The API is available at `http://localhost:8000`. Healthcheck at `GET /healthz`.

---

## Limitations

- **P0 is snapshot-only.** There is no live fetching of tender notices. All data must be explicitly imported.
- **All evaluation metrics are fixture consistency.** They measure whether the deterministic pipeline reproduces its committed expected outputs, not whether the system performs well against real tender data.
- **The corpus is synthetic.** The 120-notice evaluation corpus uses `source=synthetic_demo` and `example.invalid` URLs. No real tender notices are included in the repository.
- **Zero-cost is a fixture.** The CNY 0.00 pricing is an offline snapshot, not a live cost measurement.
- **Latency is committed, not measured.** P95 latency comes from the e2e dataset's `latency_ms` field, not from live timing.
- **Live-model runs are not reproducible.** Enabling a real model provider breaks determinism and incurs real cost.
- **Playwright E2E flows are disabled** in the P0 snapshot-only build (`test:e2e` is a no-op).
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
