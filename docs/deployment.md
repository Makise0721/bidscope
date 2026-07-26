# Deployment

**Version:** 2026-07-23
**Applies to:** BidScope P0 Docker deployment

---

## Architecture Overview

BidScope deploys as a **single Docker image** that runs one of two process roles:

| Role | Command | Responsibility |
|---|---|---|
| `api` | `bidscope api` | Serves the FastAPI backend and the built React SPA. Handles query runs, report generation, snapshot ingestion, subscription inbox, and SSE event streams. |
| `scheduler` | `bidscope scheduler start` | Runs the APScheduler loop that fires due subscription ticks. Must have **exactly one** instance per deployment (guarded by PostgreSQL advisory locks). |

Both roles share the same image, the same configuration, and the same database. They are distinguished only by the command passed to the container.

External dependencies:

- **PostgreSQL with pgvector** — primary data store for notices, snapshots, runs, reports, checkpoints, subscriptions, and evaluation results.
- **MinIO** (S3-compatible) — object storage for generated DOCX reports and snapshot payloads in production. The local filesystem is used for `demo` and `development` modes (`object_store_root`).

---

## Prerequisites

- Docker Engine 24+
- Docker Compose v2+
- 4 GB available RAM for the database container

---

## Quick Start

```bash
# 1. Start infrastructure services
docker compose up -d postgres minio

# 2. Wait for postgres to be healthy (compose handles this automatically with depends_on)
# 3. Run database migrations (one-time)
docker compose run --rm api alembic upgrade head

# 4. Create LangGraph checkpoint tables (one-time)
docker compose run --rm api bidscope checkpoints setup

# 5. Start the API server
docker compose up -d api

# 6. (Optional) Start the scheduler (one instance per deployment)
docker compose up -d scheduler
```

The API is available at `http://localhost:8000`. The healthcheck endpoint is `GET /healthz`.

---

## Environment Variables

All application configuration uses the `BIDSCOPE_` prefix. Defaults are loaded from `.env` if present.

| Variable | Default | Description |
|---|---|---|
| `BIDSCOPE_APP_MODE` | `demo` | Runtime mode: `demo`, `development`, `production`, or `test`. Test-only routes are registered only when this is `test`. |
| `BIDSCOPE_DATABASE_URL` | `postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope` | Async SQLAlchemy URL for the main application engine. |
| `BIDSCOPE_CHECKPOINT_DATABASE_URL` | `postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope` | Sync SQLAlchemy URL for LangGraph checkpoint persistence. Must use the `psycopg` driver, not bare `postgresql://` (which selects the unmaintained `psycopg2`). |
| `BIDSCOPE_REAL_MODEL_ENABLED` | `false` | Enable live-model providers. When `false`, the deterministic fake model is used. |
| `BIDSCOPE_MODEL_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible base URL for the live-model provider. |
| `BIDSCOPE_MODEL_NAME` | `deepseek-chat` | Model name passed to the live-model provider. |
| `BIDSCOPE_MODEL_API_KEY` | (unset) | API key for the live-model provider. Required when `REAL_MODEL_ENABLED=true`. |
| `BIDSCOPE_OBJECT_STORE_TYPE` | `local` | Object-store backend: `local` (filesystem) or `s3` (S3-compatible / MinIO). |
| `BIDSCOPE_OBJECT_STORE_ROOT` | `data/objects` | Root directory for the local object store (used only when `OBJECT_STORE_TYPE=local`). |
| `BIDSCOPE_S3_ENDPOINT` | (unset) | S3-compatible endpoint URL (e.g. `http://minio:9000`). Required when `OBJECT_STORE_TYPE=s3`. |
| `BIDSCOPE_S3_BUCKET` | (unset) | S3 bucket name. Required when `OBJECT_STORE_TYPE=s3`. |
| `BIDSCOPE_S3_ACCESS_KEY` | (unset) | Static access key. Required when `OBJECT_STORE_TYPE=s3` (the store never falls back to ambient credentials). |
| `BIDSCOPE_S3_SECRET_KEY` | (unset) | Static secret key paired with `S3_ACCESS_KEY`. Required when `OBJECT_STORE_TYPE=s3`. |
| `BIDSCOPE_S3_PREFIX` | (empty) | Optional logical key prefix applied to every stored object. |
| `BIDSCOPE_ADMIN_TOKEN` | (unset) | Optional token for administrative operations. |
| `BIDSCOPE_TEST_CONTROL_TOKEN` | (unset) | Token required by `/api/test-controls/*` routes (only registered in `test` mode). |

---

## Database Initialization

### Migrations

BidScope uses Alembic for schema migration. The migration scripts live in `migrations/versions/`.

```bash
# Apply all pending migrations
alembic upgrade head

# Check current revision
alembic current
```

The `compose.yaml` volume mount `./scripts/postgres-init:/docker-entrypoint-initdb.d:ro` automatically creates a `bidscope_test` database on first container start, used by the integration test suite.

### Checkpoint Tables

LangGraph's checkpoint persistence requires dedicated tables, created by:

```bash
bidscope checkpoints setup
```

This is a one-time setup step. The application never creates these tables implicitly.

### Snapshot Import

To import a verified snapshot bundle:

```bash
bidscope snapshots inspect /path/to/bundle    # validate only
bidscope snapshots import /path/to/bundle     # validate and import
```

Both commands honour `--json` for machine-readable output and never access the network.

---

## Process Roles

### `bidscope api`

Serves HTTP on port 8000. Responsibilities:

- REST API for runs, reports, subscriptions, inbox, sources, evaluations.
- Server-Sent Events (SSE) for real-time run progress.
- Static file serving for the built React SPA (mounted after API routes).
- Healthcheck at `GET /healthz`.

### `bidscope scheduler start`

Starts the APScheduler process role. Characteristics:

- Blocks the calling process (runs an infinite sleep loop with `KeyboardInterrupt` shutdown).
- Must have **exactly one** instance per deployment. PostgreSQL advisory locks prevent duplicate execution of a tick, but running multiple scheduler instances wastes resources and may cause lock contention.
- Fires due subscription ticks on the schedule defined in `bidscope.subscriptions.scheduler`.

For one-shot tick execution (tests, manual triggers):

```bash
bidscope scheduler run
```

---

## Non-Root Execution

The Dockerfile creates and switches to a non-root user:

```dockerfile
RUN useradd --uid 1000 --create-home bidscope
USER bidscope
```

All application processes run as UID 1000 inside the container. No process runs as root.

---

## Health Checks

### Container healthcheck (Docker)

Defined in the Dockerfile:

```dockerfile
HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1
```

### Application endpoint

`GET /healthz` returns:

```json
{"status": "ok", "mode": "demo"}
```

The `mode` field reflects the current `BIDSCOPE_APP_MODE`.

### Compose healthchecks

The `postgres` service uses `pg_isready`, and `minio` uses `/minio/health/live`. Application services should declare `depends_on: postgres: condition: service_healthy` to avoid startup races.

---

## MinIO Configuration

MinIO provides S3-compatible object storage for production deployments.

| Setting | Value (compose default) |
|---|---|
| API port | 9000 |
| Console port | 9001 |
| Root user | `minio` |
| Root password | `minioadmin` |

For production, replace the default credentials and configure the application to use the S3 endpoint. The `bidscope.delivery.objects` module provides the object-store abstraction; production deployments use S3 while `demo`/`development` deployments use `LocalObjectStore` writing to `object_store_root`.

To access the MinIO console: `http://localhost:9001`.

---

## Troubleshooting

### Port conflicts

If port 8000, 5432, 9000, or 9001 is already in use:

- **8000** — change the host port mapping in `compose.yaml` (e.g., `8080:8000`) or stop the conflicting process.
- **5432** — change the postgres host port mapping or stop the local PostgreSQL.
- **9000 / 9001** — change the MinIO host port mappings.

### Database connection failures

- Verify postgres is healthy: `docker compose ps postgres`
- Check credentials match between `BIDSCOPE_DATABASE_URL` and the compose `POSTGRES_*` environment variables.
- Ensure migrations have been applied: `alembic current` should show the latest revision.

### Migration failures

- Check `alembic history` to see the migration chain.
- If the database is ahead of the local migration scripts (e.g., after switching branches), run `alembic downgrade -1` to step back.
- The `migrations/versions/*.py` files are pinned to specific `revision` and `down_revision` identifiers — do not edit them after they have been applied.

### Scheduler not running

- Only one scheduler instance should run per deployment.
- Verify the database is reachable and checkpoint tables exist (`bidscope checkpoints setup`).
- Check scheduler logs for advisory lock acquisition failures, which indicate a second scheduler instance.

### SPA not loading

- The SPA is built during the Docker build stage (`web-build`) and copied into `backend/src/bidscope/static`. If that directory is missing, the API will run but the frontend will return 404.
- Rebuild the image to regenerate static assets after any frontend change.

---

## Production Notes

- Set `BIDSCOPE_APP_MODE=production` for production deployments.
- Enable and configure a live model provider (`BIDSCOPE_REAL_MODEL_ENABLED=true` with a valid API key) — but note that this breaks reproducibility and incurs real cost.
- Use a managed PostgreSQL service with pgvector support instead of the compose `postgres` service.
- Replace the compose `minio` service with a production S3-compatible endpoint.
- Run the scheduler as a separate deployment with replica count 1.
- The image runs as non-root (UID 1000) and exposes only port 8000.

---

## End-to-End Tests (Playwright)

The Playwright E2E suite exercises the full interactive + subscription flow against a real API server and a dedicated `bidscope_e2e` database (distinct from `bidscope_test`, so E2E never collides with the pytest integration suite).

### Prerequisites

- A running PostgreSQL with `pgvector` and a `bidscope_e2e` database (the `scripts/postgres-init/01_create_test_db.sql` init script creates it; for an already-running instance, `CREATE DATABASE bidscope_e2e` once).
- Node 22+ and the Playwright Chromium browser (`npx playwright install chromium`).

### Running locally

```bash
BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e
```

The webServer step (defined in `e2e/playwright.config.ts`) chains: build the SPA → reset+migrate+seed `bidscope_e2e` (`e2e/db-setup.mjs`) → start `bidscope api serve --host 127.0.0.1 --port 8001`. Both the `desktop` (1440×900) and `mobile` (390×844) projects run — 12 tests total. The token gates the test-only `/api/test-controls/*` routes (registered only in `test` mode).

### CI

The `e2e` job in `.github/workflows/ci.yml` provisions `bidscope_e2e`, installs Chromium, runs the suite, and uploads the HTML report + test results on any outcome.

