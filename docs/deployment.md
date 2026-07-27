# Deployment

**Version:** 2026-07-27
**Applies to:** BidScope P1-A Compose configuration baseline

## Production Compose Workflow

BidScope uses one image for two roles:

| Role | Command | Responsibility |
|---|---|---|
| `api` | `bidscope api serve` | Serves the FastAPI API and built SPA on container port 8000. |
| `scheduler` | `bidscope scheduler start` | Runs subscription ticks. Start exactly one scheduler per deployment. |

The API and scheduler share the same production configuration, PostgreSQL databases, and S3-compatible object store. PostgreSQL and MinIO are private Compose services; neither their database/API ports nor the MinIO console are published on the host. The API is bound to `127.0.0.1:8000` so a local reverse proxy can terminate TLS and provide the public entry point.

1. Copy `.env.production.example` to the deployment's `.env` file and replace every blank value with deployment-specific values. Do not commit that file.
2. Generate a long random `BIDSCOPE_ADMIN_TOKEN`, S3 credentials, PostgreSQL credentials, and pre-encoded DSN passwords.
3. Set `BIDSCOPE_DATABASE_URL` to a full `postgresql+asyncpg://` DSN and `BIDSCOPE_CHECKPOINT_DATABASE_URL` to a full `postgresql+psycopg://` DSN. Both must have an authority/host and database name, and must not contain query parameters or fragments. Percent-encode every reserved character in credentials before placing it in the DSN.
4. Set the active JSON `BIDSCOPE_ALLOWED_ORIGINS` and `BIDSCOPE_TRUSTED_HOSTS` values to the real public origin and host. Keep `BIDSCOPE_EXTERNAL_SCHEME=https` behind a TLS reverse proxy.
5. Validate interpolation and start the stack:

```bash
docker compose config -q
docker compose up -d postgres minio minio-init
docker compose run --rm api alembic upgrade head
docker compose run --rm api bidscope checkpoints setup
docker compose up -d api scheduler
docker compose ps
```

`api` keeps its `/healthz` HTTP healthcheck. The scheduler is not an HTTP server, so Compose explicitly disables its inherited image healthcheck; inspect its process state and logs instead.

## Required Production Variables

`compose.yaml` requires these values for both application roles:

- `BIDSCOPE_ADMIN_TOKEN`
- `BIDSCOPE_DATABASE_URL` and `BIDSCOPE_CHECKPOINT_DATABASE_URL`
- `BIDSCOPE_ALLOWED_ORIGINS`, `BIDSCOPE_TRUSTED_HOSTS`, and `BIDSCOPE_EXTERNAL_SCHEME`
- `BIDSCOPE_POSTGRES_DB`, `BIDSCOPE_POSTGRES_USER`, and `BIDSCOPE_POSTGRES_PASSWORD`
- `BIDSCOPE_S3_ENDPOINT`, `BIDSCOPE_S3_REGION`, `BIDSCOPE_S3_BUCKET`, `BIDSCOPE_S3_PREFIX`, `BIDSCOPE_S3_ACCESS_KEY`, and `BIDSCOPE_S3_SECRET_KEY`
- `BIDSCOPE_REAL_MODEL_ENABLED`; it defaults to `false` in Compose
- `BIDSCOPE_MODEL_API_KEY` when `BIDSCOPE_REAL_MODEL_ENABLED=true`

Production mode rejects the built-in demo database DSNs. It also rejects DSNs with a missing authority, missing database name, unsupported driver scheme, query string, or fragment. The two DSNs are stored as secrets in application settings and are only unwrapped at database, migration, and checkpoint driver boundaries.

## Network and Storage Boundaries

PostgreSQL, MinIO's S3 API, and the MinIO console are reachable only from the Compose network. Use a temporary operational container on that network for database or object-store administration rather than adding host `ports` mappings. MinIO root credentials are always passed from `BIDSCOPE_S3_ACCESS_KEY` and `BIDSCOPE_S3_SECRET_KEY`; Compose has no `minioadmin` fallback.

`minio-init` creates the configured bucket before API and scheduler start. Both application services wait on PostgreSQL, MinIO, and the one-shot bucket initializer.

## Process Operations

```bash
# Check API and scheduler status
docker compose ps api scheduler

# Inspect bounded application logs
docker compose logs --tail=200 api
docker compose logs --tail=200 scheduler

# Stop process roles while retaining volumes
docker compose stop api scheduler

# Start them after configuration or image updates
docker compose up -d api scheduler
```

`/healthz` proves that the API process can answer HTTP. It is intentionally not a database or object-store readiness check. Scheduler liveness is determined by its running process and scheduler logs, not by an HTTP endpoint.

## Snapshot and Test Operations

The snapshot commands remain offline:

```bash
bidscope snapshots inspect /path/to/bundle
bidscope snapshots import /path/to/bundle
```

Integration and E2E test databases must use their dedicated guarded `*_test` or `*_e2e` targets. Do not point their environment URLs at a production Compose database.
