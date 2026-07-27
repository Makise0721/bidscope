# BidScope P1-C Backup, Recovery, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide verifiable PostgreSQL/object-store backups, explicit non-destructive restore commands, Compose operations support, a repeatable recovery drill, and release gates that prove RPO 24 hours and RTO 4 hours.

**Architecture:** Backups are created by an explicit Typer `bidscope ops backup` command, never by an API request or a new daemon. A versioned manifest binds a PostgreSQL custom-format dump and every object-store object to SHA-256 hashes; local disk is the default target and external S3 replication is opt-in. Restore requires a separate empty database/object destination and an explicit confirmation flag, then reruns manifest and application smoke verification.

**Tech Stack:** Python 3.12, Typer, SQLAlchemy sync URL parsing, PostgreSQL `pg_dump`/`pg_restore`, local filesystem, boto3 S3, SHA-256, Docker Compose, shell smoke scripts, pytest, GitHub Actions.

---

## File Map

**Create**

- `backend/src/bidscope/backup.py` — manifest model, object enumeration/copy, `pg_dump`/`pg_restore` orchestration with `PGHOST`/`PGPORT`/`PGUSER`/`PGDATABASE`/`PGPASSWORD` environment injection, verify/prune/restore services.
- `backend/tests/unit/test_backup.py` — manifest hashes, object-key safety, retention, and command construction tests.
- `backend/tests/integration/test_backup_restore.py` — PostgreSQL + local object-store backup and restore verification.
- `scripts/backup_restore_smoke.sh` — clean Compose recovery drill and RPO/RTO evidence output for Linux CI/operations.
- `scripts/recovery_fixture.py` — deterministic API fixture that creates a completed report, DOCX export, subscription, and scheduler inbox event in the recovery environment.
- `docs/runbooks/bidscope-production.md` — initialization, upgrade, rollback, backup, restore, rotation, and scheduler diagnosis.

**Modify**

- `backend/src/bidscope/config.py` — local backup root, retention, optional external S3 destination, and tool timeout settings.
- `backend/src/bidscope/delivery/objects.py` — safe object listing/deletion operations for backup and prune.
- `backend/src/bidscope/api/dependencies.py` — construct backup source/destination stores from settings.
- `backend/src/bidscope/cli.py` — add `ops backup create|verify|list|prune|restore` commands, with explicit application and checkpoint restore targets.
- `Dockerfile` — install `postgresql-client` and create writable backup root for UID 1000.
- `compose.yaml` — add an `ops` profile/one-shot backup service with explicit backup volume, no long-running replica.
- `migrations/versions/*.py` — no old migration edits; only verify the current head in the backup preflight.
- `backend/tests/unit/test_cli.py` — command wiring and non-destructive restore confirmation tests.
- `backend/tests/integration/conftest.py` — include backup-created records/object roots in cleanup.
- `.env.production.example` — document local backup root, retention, and disabled-by-default external S3 replication.
- `.github/workflows/ci.yml` — install PostgreSQL client tools and run the recovery gate.
- `README.md` and `docs/deployment.md` — link the production runbook and state migration/rollback rules.

---

### Task 1: Add backup configuration and managed object-store enumeration

**Files:** `backend/tests/unit/test_cli.py`, `backend/src/bidscope/config.py`, `backend/src/bidscope/delivery/objects.py`, `backend/src/bidscope/api/dependencies.py`, `backend/tests/unit/delivery/test_objects.py`

- [ ] **Step 1: Write failing configuration and object-list tests.** Assert default `backup_root` exists as a non-empty path, daily/weekly retention defaults are 7/4, external S3 replication defaults to false, and non-positive retention values fail. Add local-store tests for deterministic `list_keys()` and `delete()` and S3 fake-client tests for paginated listing and deletion.

```python
def test_backup_defaults_are_safe() -> None:
    settings = Settings()
    assert settings.backup_daily_retention == 7
    assert settings.backup_weekly_retention == 4
    assert settings.backup_s3_enabled is False


def test_local_object_store_lists_and_deletes_keys(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("reports/a.docx", b"a")
    store.put_bytes("imports/b.html", b"b")
    assert store.list_keys() == ["imports/b.html", "reports/a.docx"]
    store.delete("imports/b.html")
    assert store.list_keys() == ["reports/a.docx"]
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_cli.py backend/tests/unit/delivery/test_objects.py -q`

Expected: FAIL because backup settings and list/delete methods do not exist.

- [ ] **Step 3: Extend the object-store protocol.** Add `list_keys() -> list[str]` and `delete(key: str) -> None` to `ObjectStore`. Implement local listing with `rglob`, normalized forward-slash relative keys, sorted output, and the existing traversal guard. Implement S3 listing with `list_objects_v2` pagination under the configured prefix and delete with `delete_object`; never accept keys containing `..` or beginning with `/`.

- [ ] **Step 4: Add backup settings.** Add `backup_root="data/backups"`, `backup_daily_retention=7`, `backup_weekly_retention=4`, `backup_s3_enabled=False`, `backup_s3_endpoint`, `backup_s3_bucket`, `backup_s3_access_key`, `backup_s3_secret_key`, `backup_s3_prefix="bidscope-backups"`, and `backup_tool_timeout_seconds=900`. Validate positive values and, when enabled, require all external S3 fields explicitly.

- [ ] **Step 5: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_cli.py backend/tests/unit/delivery/test_objects.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/config.py backend/src/bidscope/delivery/objects.py backend/src/bidscope/api/dependencies.py backend/tests/unit/test_cli.py backend/tests/unit/delivery/test_objects.py
git commit -m "feat: add managed backup storage configuration"
```

### Task 2: Implement versioned backup manifests and local creation

**Files:** `backend/src/bidscope/backup.py`, `backend/tests/unit/test_backup.py`, `backend/src/bidscope/config.py`

- [ ] **Step 1: Write failing manifest tests.** Test that a manifest records `backup_version`, UTC `created_at`, app version, git commit, migration revision, database dump path/hash, object key/size/hash, counts, and retention class. Test that changing one dump byte or object byte makes verification fail. Test that manifest paths cannot escape the backup directory.

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `uv run pytest backend/tests/unit/test_backup.py -q`

Expected: FAIL because `bidscope.backup` does not exist.

- [ ] **Step 3: Define the concrete manifest types.** Use Pydantic models equivalent to:

```python
class BackupObject(BaseModel):
    key: str
    archive_path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

class BackupManifest(BaseModel):
    backup_version: Literal["p1-v1"]
    backup_id: str
    created_at: AwareDatetime
    app_version: str
    git_commit: str
    migration_revisions: dict[str, str]
    database_dumps: dict[str, str]
    database_dump_sha256: dict[str, str]
    objects: list[BackupObject]
    counts: dict[str, int]
    retention_class: Literal["daily", "weekly"]
```

Add `sha256_file`, `safe_backup_path`, `write_manifest_atomic`, and `verify_manifest` helpers. Normalize the application and checkpoint connection identities without passwords: when they identify the same PostgreSQL database, write one `<backup_root>/<backup_id>/application.dump` and map both manifest roles to that artifact; when they differ, write `application.dump` and `checkpoint.dump`. Store object bytes as `<backup_root>/<backup_id>/objects/object-000001.bin`; object keys remain only in the manifest, so arbitrary source keys cannot become filesystem paths.

- [ ] **Step 4: Implement backup creation.** `BackupService.create(retention_class)` must:
  1. create a unique UTC backup directory under `backup_root`;
  2. identify whether `BIDSCOPE_DATABASE_URL` and `BIDSCOPE_CHECKPOINT_DATABASE_URL` refer to the same PostgreSQL database; query `alembic_version` once for a shared database or once per distinct database, require exactly one current revision for each, and record the role-to-dump mapping and revisions in the manifest;
  3. parse each distinct configured PostgreSQL URL and invoke one `pg_dump --format=custom` per distinct database, writing `application.dump` and, only for a distinct checkpoint database, `checkpoint.dump`; pass each connection through `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, and a process-only `PGPASSWORD` environment value, never placing a credential-bearing DSN in argv or logs;
  4. enumerate object keys, copy bytes into numbered archive files, and hash each file;
  5. write and atomically rename `manifest.json` only after all hashes match;
  6. optionally replicate the complete directory to external S3 only when `backup_s3_enabled=true`;
  7. return a JSON-safe summary with backup ID, path, counts, and verification status.

- [ ] **Step 5: Add command-runner injection.** Accept a `CommandRunner` protocol in `BackupService` so unit tests can assert exact `pg_dump` arguments and simulate non-zero exit codes without invoking a real database. Convert tool failures to `BackupError(code="backup_database_dump_failed")` with no command output containing secrets.

- [ ] **Step 6: Run unit tests and commit.**

Run: `uv run pytest backend/tests/unit/test_backup.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/backup.py backend/tests/unit/test_backup.py
git commit -m "feat: create verifiable local backups"
```

### Task 3: Add external S3 replication and retention pruning

**Files:** `backend/src/bidscope/backup.py`, `backend/src/bidscope/delivery/objects.py`, `backend/tests/unit/test_backup.py`, `backend/tests/unit/delivery/test_objects.py`

- [ ] **Step 1: Write failing replication/prune tests.** Use a fake S3 client to assert every manifest, dump, and object archive is uploaded under `backup_s3_prefix/<backup_id>/`. Create daily and weekly backup directories with controlled timestamps; assert `prune()` keeps the newest 7 daily and newest 4 weekly backups and never removes the newest valid backup overall.

- [ ] **Step 2: Implement explicit replication.** Build a separate `S3ObjectStore` for the backup destination from the `backup_s3_*` settings, not from the application object-store prefix. Upload only after local manifest verification. Verify the remote manifest and each remote object by reading back size/hash when the provider supports it; mark replication failure as a failed backup result rather than deleting the valid local backup.

- [ ] **Step 3: Implement list/verify/prune.** `BackupService.list()` returns only manifests that parse and whose local hashes verify, with invalid entries marked `invalid`. `prune()` deletes only backup directories classified as daily/weekly and older than the configured retained set; it refuses to delete a directory whose manifest is invalid or the newest valid directory. External S3 pruning uses the same backup IDs after remote manifest verification.

- [ ] **Step 4: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_backup.py backend/tests/unit/delivery/test_objects.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/backup.py backend/src/bidscope/delivery/objects.py backend/tests/unit/test_backup.py backend/tests/unit/delivery/test_objects.py
git commit -m "feat: replicate and prune verified backups"
```

### Task 4: Add non-destructive restore and post-restore verification

**Files:** `backend/src/bidscope/backup.py`, `backend/tests/unit/test_backup.py`, `backend/tests/integration/test_backup_restore.py`, `backend/tests/unit/test_cli.py`

- [ ] **Step 1: Write failing restore safety tests.** Assert restore rejects a missing `--confirm` flag, a target object root that already contains files, a target database whose `alembic_version` is present, an invalid manifest, and a path outside the configured backup root unless an explicit backup path is passed. Assert `pg_restore` is invoked without `--clean` or `--if-exists`.

- [ ] **Step 2: Implement restore preflight.** `BackupService.restore(backup_dir, target_database_url, target_checkpoint_database_url, target_object_root, confirmed)` must:
  - require `confirmed is True`;
  - run `verify_manifest` before any target write;
  - require an empty target object root;
  - identify whether the two target URLs refer to one shared empty database or two distinct empty databases, and require each distinct database to have no `alembic_version` table;
  - validate both target database URLs and parse fields without logging credentials;
  - restore each distinct manifest dump exactly once with `pg_restore --exit-on-error --no-owner --dbname=<target database name>`, passing `PGHOST`, `PGPORT`, `PGUSER`, and process-only `PGPASSWORD` values, without `--clean` or `--if-exists`;
  - copy archive objects to the empty target root and verify every hash;
  - query `alembic_version` in each restored database, compare with `manifest.migration_revisions`, and return a bounded summary.

- [ ] **Step 3: Implement integration verification.** Use the normal shared application/checkpoint test database as the primary case: seed it with a synthetic snapshot, run/report/DOCX and subscription records, create a backup against a temporary local object root, restore into one second empty database/root, and assert:
  - old report claims still resolve to the original evidence IDs and content hashes;
  - the DOCX object exists and bytes hash identically;
  - subscription seen items and audit rows exist;
  - a new run can be created after restore;
  - the restored migration revision equals the manifest revision.

- [ ] **Step 4: Add CLI command tests.** Mock `BackupService.restore` and assert `bidscope ops backup restore` refuses to call it without `--confirm`, passes explicit target arguments with `--confirm`, and prints JSON without credentials.

- [ ] **Step 5: Run tests and commit.**

Run: `uv run pytest backend/tests/unit/test_backup.py backend/tests/unit/test_cli.py backend/tests/integration/test_backup_restore.py -q`

Expected: PASS. The integration command requires `pg_dump`, `pg_restore`, and a PostgreSQL service.

```bash
git add backend/src/bidscope/backup.py backend/tests/unit/test_backup.py backend/tests/unit/test_cli.py backend/tests/integration/test_backup_restore.py
git commit -m "feat: restore backups without destructive overwrite"
```

### Task 5: Expose explicit backup CLI commands

**Files:** `backend/src/bidscope/cli.py`, `backend/tests/unit/test_cli.py`, `README.md`

- [ ] **Step 1: Write failing CLI tests.** Use Typer `CliRunner` to assert the help exposes `ops backup create`, `verify`, `list`, `prune`, and `restore`. Assert `--json` output contains only stable fields and restore requires `--confirm`.

- [ ] **Step 2: Add the command hierarchy.** Register:

```python
ops_app = typer.Typer(help="Production operations.", no_args_is_help=True)
backup_app = typer.Typer(help="Backup and restore operations.", no_args_is_help=True)
app.add_typer(ops_app, name="ops")
ops_app.add_typer(backup_app, name="backup")
```

Implement commands with explicit options:

- `create --retention-class daily|weekly [--json]`
- `verify BACKUP_DIR [--json]`
- `list [--json]`
- `prune [--json]`
- `restore BACKUP_DIR --target-database-url URL --target-checkpoint-database-url URL --target-object-root PATH --confirm [--json]`

Use `configure_windows_selector_event_loop_policy()` before async operations. Catch `BackupError`, print a bounded message/JSON error, and exit 1. Do not echo the target URL if it contains credentials.

- [ ] **Step 3: Run CLI tests and commit.**

Run: `uv run pytest backend/tests/unit/test_cli.py -q`

Expected: PASS.

```bash
git add backend/src/bidscope/cli.py backend/tests/unit/test_cli.py README.md
git commit -m "feat: expose backup and restore operations"
```

### Task 6: Package operations support in Docker Compose

**Files:** `Dockerfile`, `compose.yaml`, `.env.production.example`, `docs/deployment.md`, `docs/runbooks/bidscope-production.md`

- [ ] **Step 1: Add PostgreSQL client tooling to the image.** Install the distribution `postgresql-client` package alongside curl, create `/app/data/backups`, and grant UID 1000 ownership. Keep the image non-root at runtime.

- [ ] **Step 2: Add an ops Compose profile.** Define a one-shot `backup` service using the same image and production environment, mounted volumes for `./data/backups:/app/data/backups` and the configured local object root if local storage is used, with command `bidscope ops backup create --retention-class daily`. Put it under `profiles: ["ops"]` so `docker compose up` does not start it and no long-running backup daemon is introduced.

- [ ] **Step 3: Document safe commands.** Add exact commands:

```bash
docker compose --profile ops run --rm backup
# Verify the newest backup before pruning.
docker compose run --rm api bidscope ops backup verify /app/data/backups/<backup-id>
docker compose run --rm api bidscope ops backup prune
```

Document that the external S3 destination is disabled unless all `BIDSCOPE_BACKUP_S3_*` values and `BIDSCOPE_BACKUP_S3_ENABLED=true` are set. State that production secrets come from an untracked env/secret file.

- [ ] **Step 4: Run image/config checks and commit.**

Run: `docker compose config`

Expected: exit 0 and the backup service appears only under the `ops` profile.

```bash
git add Dockerfile compose.yaml .env.production.example docs/deployment.md docs/runbooks/bidscope-production.md
git commit -m "ops: package backup commands for Compose"
```

### Task 7: Add the repeatable recovery drill and release gates

**Files:** `scripts/backup_restore_smoke.sh`, `.github/workflows/ci.yml`, `backend/tests/integration/test_backup_restore.py`, `README.md`, `docs/runbooks/bidscope-production.md`

- [ ] **Step 1: Write the recovery script contract.** The script must use a temporary Compose project name and temporary backup/object directories, set a unique `BIDSCOPE_TEST_CONTROL_TOKEN`, record `started_at` and `backup_created_at`, and fail on any command error. It must execute this exact sequence:

```bash
docker compose -p "bidscope-recovery-${RUN_ID}" up -d postgres minio minio-init
docker compose -p "bidscope-recovery-${RUN_ID}" run --rm api alembic upgrade head
docker compose -p "bidscope-recovery-${RUN_ID}" run --rm api bidscope checkpoints setup
docker compose -p "bidscope-recovery-${RUN_ID}" run --rm api bidscope snapshots import data/demo/batch-1
# Start the API in test mode, then use the existing E2E fixture sequence:
# create a run, approve it, fetch its report/DOCX, create a subscription,
# and invoke POST /api/test-controls/run-scheduler-tick with
# X-Test-Control-Token=$BIDSCOPE_TEST_CONTROL_TOKEN.
docker compose -p "bidscope-recovery-${RUN_ID}" run --rm api bidscope ops backup create --retention-class daily --json
# Destroy only the recovery project's data volumes, then restore to fresh targets.
docker compose -p "bidscope-recovery-${RUN_ID}" down -v
# Start empty target services, restore with --confirm, then run /readyz,
# fetch the restored report/DOCX, create a new run, and invoke one scheduler tick.
```

The script must print JSON evidence containing backup ID, manifest hash, restore duration, backup age, `rpo_hours`, `rto_seconds`, and `passed`; it must exit non-zero when `rpo_hours > 24` or `rto_seconds > 14400`.

- [ ] **Step 2: Add the application verification step.** After restore, call `/readyz`, create a new run, fetch a restored report and DOCX, and run one scheduler tick. Verify the old report's evidence version and the new run's completion before emitting `passed=true`.

- [ ] **Step 3: Add CI dependencies and recovery job.** Install `postgresql-client` in the CI runner or use the built image for `pg_dump`/`pg_restore`. Add a `recovery` job after Docker/E2E prerequisites, run `scripts/backup_restore_smoke.sh`, and upload the JSON evidence as an artifact. Do not make recovery evidence depend on external public websites or live model providers.

- [ ] **Step 4: Document RPO/RTO evidence and rollback.** Add the recovery artifact path and interpretation to the runbook. Document that application image rollback is allowed only after migration compatibility checks; database downgrade is never automatic. Add pre-release ordering: backup -> compatibility check -> migration -> smoke -> publish.

- [ ] **Step 5: Run the P1-C gate and commit.**

Run: `uv run pytest backend/tests/unit/test_backup.py backend/tests/unit/test_cli.py backend/tests/integration/test_backup_restore.py -q`

Run: `bash scripts/backup_restore_smoke.sh`

Run: `docker compose config`

Expected: all commands exit 0; recovery JSON contains `"passed": true`, `rpo_hours <= 24`, and `rto_seconds <= 14400`.

```bash
git add scripts/backup_restore_smoke.sh .github/workflows/ci.yml backend/tests/integration/test_backup_restore.py README.md docs/runbooks/bidscope-production.md
git commit -m "ci: enforce backup recovery and release gates"
```

**P1-C gate:** local backup creation/verification/prune works, optional external replication is explicit, restore cannot overwrite an online target, a clean Compose recovery drill passes, and release evidence proves the RPO/RTO targets.
