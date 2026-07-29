# Deployment

**Version:** 2026-07-28
**Applies to:** BidScope P1-A Compose security and configuration baseline and P1-C operations packaging

For the complete production procedure, including migrations, upgrades/rollback,
backup verification/pruning/restoration, key rotation, and scheduler diagnosis,
use [`docs/runbooks/bidscope-production.md`](runbooks/bidscope-production.md).

The image includes `pg_dump`/`pg_restore` from the distribution
`postgresql-client` package and creates `/app/data/backups` writable by runtime
UID 1000. Backups are never a daemon: run the one-shot ops profile explicitly.

```bash
docker compose --profile ops run --rm backup
# Verify the newest backup before pruning.
docker compose --profile ops run --rm backup bidscope ops backup verify /app/data/backups/<backup-id>
docker compose --profile ops run --rm backup bidscope ops backup prune
```

The `backup` service is not rendered by ordinary `docker compose up`; it is
available only with `--profile ops`. External backup S3 replication remains
disabled unless `BIDSCOPE_BACKUP_S3_ENABLED=true` and every
`BIDSCOPE_BACKUP_S3_*` value is configured in the protected deployment env.

## P1-A Security Contract

P1-A is the production security and configuration layer. It is deliberately single-tenant: the application credential is one operator-managed Admin Token, not a user-account or RBAC system.

### Startup must fail closed

The production settings validator rejects startup when any of the following is missing or unsafe:

- `BIDSCOPE_APP_MODE` is not `production`.
- `BIDSCOPE_ADMIN_TOKEN` is blank, shorter than `BIDSCOPE_ADMIN_TOKEN_MIN_LENGTH` (32 by default), longer than the bounded header limit, or equal to a placeholder such as `change-me`, `replace-me`, `your-admin-token`, or `minioadmin`.
- `BIDSCOPE_OBJECT_STORE_TYPE` is not `s3`, or any of `BIDSCOPE_S3_ENDPOINT`, `BIDSCOPE_S3_REGION`, `BIDSCOPE_S3_BUCKET`, `BIDSCOPE_S3_ACCESS_KEY`, or `BIDSCOPE_S3_SECRET_KEY` is blank. The application does not fall back to ambient S3 credentials.
- `BIDSCOPE_REAL_MODEL_ENABLED=true` without `BIDSCOPE_MODEL_API_KEY`.
- `BIDSCOPE_ALLOWED_ORIGINS` is empty, wildcarded, or contains a path, query, fragment, or user-info component; `BIDSCOPE_TRUSTED_HOSTS` is empty or wildcarded; or `BIDSCOPE_EXTERNAL_SCHEME` is not `https`.
- Either PostgreSQL DSN uses the wrong explicit driver, omitted credentials/authority/database, a demo default, a fragment, an unknown or target-overriding query parameter, or a TLS query other than `ssl=require` for the asyncpg DSN or `sslmode=require` for the psycopg DSN.

Validation errors are presented as a bounded startup marker and do not print DSNs, passwords, Admin Tokens, model keys, tracebacks, or Pydantic internals.

### Endpoint authorization

The application transport and router boundaries enforce this matrix:

| Endpoint area | Policy |
|---|---|
| `/healthz` | Public process liveness only; it does not prove database or object-store readiness |
| `/assets/*` and SPA GET | Public unless the reverse proxy limits the entry point; no sensitive data is embedded in the static entry |
| `/api/runs/*`, `/api/reports/*`, `/api/subscriptions/*`, `/api/inbox-events`, `/api/sources/*`, `/api/evaluations/*` | Require `X-Admin-Token` |
| `/api/test-controls/*` | Only registered in `BIDSCOPE_APP_MODE=test`; requires `X-Test-Control-Token` and is never a production route |

Missing, empty, wrong, or oversized Admin Token headers receive `401` with `{"detail":"invalid admin token"}`. The value is compared against the configured token and is not echoed in errors. Trusted Host rejects unconfigured hosts, and CORS accepts only the explicit configured origins with credentials disabled. The application does not infer a trusted host, external scheme, or origin from arbitrary request headers.

### SPA token handling and rotation

1. Open the SPA at the configured same-origin public URL.
2. Enter the Admin Token in the Workbench **Admin token** control and choose **Save**.
3. The browser trims the value and stores it only in the current tab's `sessionStorage`; it sends the value as `X-Admin-Token` on API JSON requests and the fetch-based authenticated SSE stream.
4. The token is not placed in `localStorage`, cookies, URLs, query strings, hashes, bundles, logs, request bodies, or reports. **Clear** removes it from the current tab.
5. A `401` clears the current tab's token and returns the UI to the authentication-required state. Enter the token again after correcting the credential.

For rotation, generate a new random token, update the deployment secret, restart both roles, and replace the token in every open browser tab. Do not commit `.env` or secret files. Existing tabs continue to send their old token until cleared or replaced, so rotate during a controlled maintenance window.

### Audit boundary

Critical run creation/confirmation/retry, subscription creation/pause/resume, snapshot import, and report/DOCX operations create bounded audit metadata. Critical mutation audit rows are flushed in the same database transaction as the business operation; a failure prevents the mutation from committing. Read/download observations may be recorded separately and do not block the successful response if audit persistence fails. Audit records contain normalized paths, request and business IDs, outcome, error code, status, and bounded allowlisted details. They never contain Admin Tokens, `Authorization` headers, model API keys, cookies/session values, raw request headers, request bodies, or report bodies.

P1-B adds `/readyz` as the dependency readiness probe and `/metrics` as a bounded, Admin Token-protected Prometheus endpoint. The container healthcheck uses `/readyz`; `/healthz` remains process liveness only. Runtime logs include a bounded request ID, normalized path, status, duration, and exception type. Capacity exhaustion returns HTTP 429 with `Retry-After: 5`; scheduler ticks and SSE lifecycle are diagnosed through bounded logs/metrics.

## Production Compose Workflow

BidScope uses one image for two roles:

| Role | Command | Responsibility |
|---|---|---|
| `api` | `bidscope api serve` | Serves the FastAPI API and built SPA on container port 8000. |
| `scheduler` | `bidscope scheduler start` | Runs subscription ticks. Start exactly one scheduler per deployment. |

The API and scheduler share the same production configuration, PostgreSQL databases, and S3-compatible object store. PostgreSQL and MinIO are private Compose services; neither their database/API ports nor the MinIO console are published on the host. The API is bound to `127.0.0.1:8000` so a local reverse proxy can terminate TLS and provide the public entry point.

1. Copy `.env.production.example` to the deployment's `.env` file and replace every blank value with deployment-specific values. Do not commit that file.
2. Generate a long random `BIDSCOPE_ADMIN_TOKEN`, S3 credentials, PostgreSQL credentials, and pre-encoded DSN passwords.
3. Set `BIDSCOPE_DATABASE_URL` to a full `postgresql+asyncpg://` DSN and `BIDSCOPE_CHECKPOINT_DATABASE_URL` to a full `postgresql+psycopg://` DSN. Both must explicitly contain a non-empty username, a non-empty password, authority/host, and database name. Percent-encode every reserved character in credentials before placing it in the DSN. The only accepted query is `?ssl=require` for the asyncpg application DSN or `?sslmode=require` for the psycopg checkpoint DSN; target override and credential keys (`host`, `port`, `database`, `dbname`, `service`, `user`, `password`, `passfile`) and all unknown keys are rejected. Fragments are rejected.
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

`api` uses `/readyz` for its HTTP healthcheck. The scheduler is not an HTTP server, so Compose explicitly disables its inherited image healthcheck; inspect its process state and bounded tick logs instead.

## Required Production Variables

`compose.yaml` requires these values for both application roles:

- `BIDSCOPE_ADMIN_TOKEN`
- `BIDSCOPE_DATABASE_URL` and `BIDSCOPE_CHECKPOINT_DATABASE_URL`
- `BIDSCOPE_ALLOWED_ORIGINS`, `BIDSCOPE_TRUSTED_HOSTS`, and `BIDSCOPE_EXTERNAL_SCHEME`
- `BIDSCOPE_POSTGRES_DB`, `BIDSCOPE_POSTGRES_USER`, and `BIDSCOPE_POSTGRES_PASSWORD`
- `BIDSCOPE_S3_ENDPOINT`, `BIDSCOPE_S3_REGION`, `BIDSCOPE_S3_BUCKET`, `BIDSCOPE_S3_PREFIX`, `BIDSCOPE_S3_ACCESS_KEY`, and `BIDSCOPE_S3_SECRET_KEY`
- `BIDSCOPE_REAL_MODEL_ENABLED`; it defaults to `false` in Compose
- `BIDSCOPE_MODEL_API_KEY` when `BIDSCOPE_REAL_MODEL_ENABLED=true`
- `BIDSCOPE_MODEL_BASE_URL` and `BIDSCOPE_MODEL_NAME`; Compose passes both values to API and scheduler, defaulting to the Settings values when unset

Production mode rejects the built-in demo database DSNs. It also rejects DSNs with missing explicit credentials, missing authority, missing database name, unsupported driver scheme, target-overriding or unknown query parameters, or fragments. The two DSNs are stored as secrets in application settings and are only unwrapped at database, migration, and checkpoint driver boundaries.

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

`/healthz` proves that the API process can answer HTTP. `/readyz` is the dependency gate used by the container healthcheck; it checks configuration, PostgreSQL, checkpoint, and object storage without returning connection details. Scheduler liveness is determined by its running process and tick logs/metrics, not by an HTTP endpoint.

## Snapshot and Test Operations

The snapshot commands remain offline:

```bash
bidscope snapshots inspect /path/to/bundle
bidscope snapshots import /path/to/bundle
```

Integration and E2E test databases must use their dedicated guarded `*_test` or `*_e2e` targets. Do not point their environment URLs at a production Compose database.

## Productization Staging Acceptance

The first productization pilot remains a single-tenant, snapshot-only CCGP
workflow. Before importing an authorized weekly batch into staging, confirm
that the external governance record exists and that the bundle uses schema
version 2 with an approved `data_contract` and explicit `batch_id`:

```bash
bidscope snapshots inspect /controlled/staging/ccgp-batch-20260729 --json
bidscope snapshots import /controlled/staging/ccgp-batch-20260729 --json
```

The inspect response must contain `"disposition": "accepted"`. A
`"disposition": "quarantined"` result is a hard admission failure; do not
move the bundle into the object store or retry by changing the source URL.
The import response records `bundle_hash`, `notice_count`, payload-file count,
warnings and the idempotent reprocessing mode. Reusing the same bundle must
return the same import ID and must not create another notice version.

Restricted real-data evaluation is a separate staging artifact flow and never
changes the deterministic CI gate:

```bash
bidscope eval validate-real \
  --manifest /controlled/staging/evaluation/dataset-manifest.json \
  --result /controlled/staging/evaluation/result.json \
  --json
```

The validator checks dataset/version linkage, snapshot IDs, exact manifest
SHA-256, bounded model metadata, sample count and citation/provenance hard-gate
outcomes. A valid completed result reports `status=validated` and
`release_decision=review_required`; it does not emit or reuse
`deterministic_target_pass`. A failed or hard-gate-invalid result is blocked.
The command performs no network access and does not run a model.

Do not publish product-quality or availability claims until the staging record
also includes: approved governance metadata, reproducible evaluation evidence,
human-usefulness review, bounded live-model cost/latency evidence if enabled,
an external-backup verification, and a clean-host recovery result within the
documented RPO/RTO thresholds. CI and E2E fixtures remain synthetic.
