# BidScope Production Runbook

This runbook applies to the production Compose stack. Use a deployment-specific,
untracked `.env` or a secret-file mechanism for all production values. Never put
production credentials in `.env.production.example`, shell history, logs, or
this runbook.

## 1. Initialize a Host

1. Install Docker Engine and Docker Compose v2, and make sure the operator can
   run `docker compose`.
2. Check out the exact application release and copy the example environment:

```bash
cp .env.production.example .env
chmod 600 .env
```

3. Replace every blank required value. Generate a high-entropy
   `BIDSCOPE_ADMIN_TOKEN`, PostgreSQL credentials, S3 credentials, and
   pre-encoded application and checkpoint DSNs. Keep the application DSN on
   `postgresql+asyncpg` and the checkpoint DSN on `postgresql+psycopg`.
4. Create the host directories used by the ops profile and verify their owner
   and mode. The container runs as UID 1000:

```bash
mkdir -p data/backups data/objects
chmod 700 data/backups data/objects
```

5. Render the Compose model before starting anything. This catches missing
   required values without starting services:

```bash
docker compose config -q
```

The production Compose model does not publish PostgreSQL or MinIO ports. The
API is published only on `127.0.0.1:8000` for a local TLS reverse proxy.

## 2. Initialize and Start

Initialize infrastructure, then apply schema and checkpoint setup explicitly:

```bash
docker compose up -d postgres minio minio-init
docker compose run --rm api alembic upgrade head
docker compose run --rm api bidscope checkpoints setup
docker compose up -d api scheduler
docker compose ps
```

`api` and `scheduler` use the same image and production settings. `scheduler`
is one process role and must have exactly one active instance for a deployment.
The `backup` service is excluded from ordinary `docker compose up` because it
has the `ops` profile and is a one-shot command, not a daemon.

## 3. Start, Stop, Status, and Logs

```bash
# Start the application roles after infrastructure is healthy.
docker compose up -d api scheduler

# Stop application roles while retaining volumes.
docker compose stop api scheduler

# Stop and remove containers while retaining named volumes.
docker compose down

# Inspect state and bounded logs.
docker compose ps

docker compose logs --tail=200 api
docker compose logs --tail=200 scheduler
```

`/healthz` is the public API process liveness check. `/readyz` is the dependency readiness gate used by the container healthcheck and checks configuration, PostgreSQL, checkpoint, and object storage without exposing connection details. The scheduler is not an HTTP server; diagnose it from process state, bounded logs/metrics, and database lock/tick evidence.

## 4. Migrations

Run migrations as an explicit release step before starting the new application
image:

```bash
docker compose run --rm api alembic upgrade head
```

Review the target migration and check the current revision before applying it:

```bash
docker compose run --rm api alembic current
docker compose run --rm api alembic history
```

Migrations must be forward-compatible with the currently deployed application.
Do not run migrations from a developer workstation against production. Do not
run `alembic downgrade` as part of a normal rollback. A migration is not
considered reversible merely because Alembic has a downgrade function.

## 5. Upgrade and Rollback

Use this order for an upgrade:

1. Create and verify a fresh backup.
2. Check application and migration compatibility.
3. Pull/build the immutable target image.
4. Run `alembic upgrade head` as an explicit step.
5. Start the target `api` and the single `scheduler` role.
6. Check `/readyz`, logs, and one representative application operation.

Example:

```bash
docker compose --profile ops run --rm backup
docker compose run --rm api bidscope ops backup verify /app/data/backups/<backup-id>
docker compose run --rm api alembic upgrade head
docker compose up -d api scheduler
docker compose ps
docker compose logs --tail=200 api scheduler
```

For an application regression, first stop the application roles and redeploy the
previous immutable image tag. A rollback is permitted only after checking that
the already-applied schema is readable by that image. Database downgrade is
never automatic: **不自动 downgrade**. If the old image is incompatible with
the current schema, restore a verified backup into fresh target databases and a
fresh object root, then point a separately reviewed deployment at those targets.
Never use `--clean` or `--if-exists` against a production target as an ad hoc
rollback mechanism.

## 6. Create, Verify, Prune, and Restore Backups

The backup operation is explicit and one-shot. It does not start with ordinary
Compose services and it never runs as a scheduler or daemon:

```bash
# Create a daily backup in the ops profile.
docker compose --profile ops run --rm backup

# Verify a selected manifest and every referenced local artifact.
docker compose run --rm api bidscope ops backup verify /app/data/backups/<backup-id>

# Prune according to configured daily/weekly retention after verification.
docker compose run --rm api bidscope ops backup prune
```

Verify the newest backup before pruning. Do not prune when manifest verification
fails, when the backup is the newest valid backup, or when the backup directory
is not under the configured backup root. Keep the backup manifest and its dump
and object files together. The container expects `/app/data/backups` and the
host path is `./data/backups`; the image creates the path writable by UID 1000.

Restore is non-destructive and targets empty destinations. Stop application
roles first, verify the source backup, and supply an explicit confirmation and
fresh database/object destinations:

```bash
docker compose stop api scheduler
docker compose run --rm api bidscope ops backup verify /app/data/backups/<backup-id>
docker compose run --rm api bidscope ops backup restore \
  /app/data/backups/<backup-id> \
  --target-database-url "$BIDSCOPE_DATABASE_URL" \
  --target-checkpoint-database-url "$BIDSCOPE_CHECKPOINT_DATABASE_URL" \
  --target-object-root /app/data/restore-objects \
  --confirm
```

The restore target must be a fresh empty database (or databases) and an empty
object root. Perform a post-restore migration/current-revision check and an
application smoke check before directing traffic to it. The restore command
must not overwrite an existing target, and production credentials must be
provided through the operator's protected environment rather than copied into
command output or logs.

External backup S3 replication is disabled unless
`BIDSCOPE_BACKUP_S3_ENABLED=true` **and** all of
`BIDSCOPE_BACKUP_S3_ENDPOINT`, `BIDSCOPE_BACKUP_S3_REGION`,
`BIDSCOPE_BACKUP_S3_BUCKET`, `BIDSCOPE_BACKUP_S3_PREFIX`,
`BIDSCOPE_BACKUP_S3_ACCESS_KEY`, and `BIDSCOPE_BACKUP_S3_SECRET_KEY` are set.
The backup destination settings are separate from the application object-store
settings. Verify the local manifest before treating a replicated copy as
usable.

## 7. Secret and Key Rotation (密钥轮换)

Rotate one credential class at a time and keep the replacement available until
all consumers have restarted successfully:

1. Generate the replacement admin, PostgreSQL, S3, or model-provider secret in
   the deployment secret manager.
2. Update the protected `.env`/secret source and validate with
   `docker compose config -q`.
3. For PostgreSQL or S3, provision the new credential with overlap where the
   provider supports it, then restart `api`, `scheduler`, and any one-shot ops
   container.
4. Check health, scheduler logs, one read/write operation, and a backup verify.
5. Revoke the old credential only after the checks pass.

Rotate `BIDSCOPE_ADMIN_TOKEN` as a coordinated cutover: clients must receive the
new token before the old token is revoked. Never print either token while
validating configuration. If a credential may have leaked, revoke it first,
then restore service with the replacement and review audit/provider logs.

## 8. Scheduler Diagnostics

Check that exactly one scheduler container is running and inspect recent logs:

```bash
docker compose ps scheduler
docker compose logs --since=15m --tail=200 scheduler
```

Look for startup configuration errors, database connection failures, advisory
lock contention, tick timeout/failure counters, and a recent successful tick.
The scheduler intentionally has no HTTP healthcheck. It must not be replaced by
starting a second scheduler: a second instance should lose the database lock
and remain visible in logs. If it is stuck, stop it, inspect the last persisted
run/lock state, and restart one instance after dependencies are healthy.

## 9. Release Safety Checklist

Before publishing:

- `docker compose config -q` succeeds with the protected production env.
- A fresh backup was created and `backup verify` succeeded.
- The target image and migrations were checked for forward compatibility.
- `alembic upgrade head` completed before application traffic was enabled.
- API readiness, scheduler process/tick evidence, and a smoke operation passed.
- Rollback means application image rollback only when schema compatibility is
  confirmed; migration downgrade is a deliberate recovery operation and is not
  automated.
