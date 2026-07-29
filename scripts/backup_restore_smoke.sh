#!/usr/bin/env bash
# Clean-host recovery drill.  Uses only committed synthetic data and the fake
# model; no public websites, model provider, or external backup credentials.
set -euo pipefail

EVIDENCE_PATH="${BIDSCOPE_RECOVERY_EVIDENCE_PATH:-recovery-evidence.json}"
SCRIPT_DIR=""
PROJECT_ROOT=""
RUN_ID=""
PROJECT_NAME=""
TEMP_ROOT=""
BACKUP_DIR=""
SOURCE_OBJECT_DIR=""
TARGET_OBJECT_DIR=""
COMPOSE_FILE=""
RECOVERY_COMPOSE_FILE=""
started_at=""
backup_created_at=""
manifest_hash=""
backup_id=""
rpo_hours=""
rto_seconds=""
old_evidence_version=""
current_step="bootstrap"
evidence_emitted=false

compose() {
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" -f "${RECOVERY_COMPOSE_FILE}" "$@" >&2
}

compose_capture() {
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" -f "${RECOVERY_COMPOSE_FILE}" "$@"
}

emit_evidence() {
  local passed="$1"
  local error_context="${2:-}"
  local exit_code="${3:-0}"
  local payload
  payload="$(python3 - "${backup_id}" "${manifest_hash}" "${started_at}" "${backup_created_at}" "${old_evidence_version}" "${rpo_hours:-null}" "${rto_seconds:-null}" "${passed}" "${error_context}" "${exit_code}" <<'PY'
import json
import sys
from datetime import datetime, timezone

backup_id, manifest_hash, started_at, backup_created_at, evidence_version, rpo, rto, passed, error, exit_code = sys.argv[1:]
payload = {
    "backup_id": backup_id or None,
    "manifest_hash": manifest_hash or None,
    "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    "backup_created_at": backup_created_at or None,
    "old_report_evidence_version": evidence_version or None,
    "rpo_hours": None if rpo == "null" else float(rpo),
    "rto_seconds": None if rto == "null" else float(rto),
    "backup_age_seconds": None if rpo == "null" else float(rpo) * 3600,
    "restore_duration_seconds": None if rto == "null" else float(rto),
    "passed": passed == "true",
    "error": error or None,
    "exit_code": int(exit_code),
}
print(json.dumps(payload, sort_keys=True))
PY
)"
  printf '%s\n' "${payload}"
  if [[ -n "${EVIDENCE_PATH}" ]]; then
    printf '%s\n' "${payload}" > "${EVIDENCE_PATH}"
  fi
  evidence_emitted=true
}

cleanup() {
  if [[ -n "${PROJECT_NAME}" && -n "${COMPOSE_FILE}" && -n "${RECOVERY_COMPOSE_FILE}" ]]; then
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" -f "${RECOVERY_COMPOSE_FILE}" \
      down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  if [[ -n "${TEMP_ROOT}" && -d "${TEMP_ROOT}" ]]; then
    rm -rf -- "${TEMP_ROOT}" || true
  fi
}

on_exit() {
  local exit_code="$1"
  trap - EXIT
  if [[ "${exit_code}" -ne 0 && "${evidence_emitted}" != true ]]; then
    emit_evidence false "${current_step}" "${exit_code}" || true
  fi
  cleanup
  exit "${exit_code}"
}
trap 'on_exit $?' EXIT

current_step="resolve-script-directory"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"
RECOVERY_COMPOSE_FILE="${SCRIPT_DIR}/recovery-compose.override.yaml"
cd "${PROJECT_ROOT}"

current_step="initialize-timestamps"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
PROJECT_NAME="bidscope-recovery-${RUN_ID}"
current_step="temporary-workspace"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/bidscope-recovery.XXXXXX")"
BACKUP_DIR="${TEMP_ROOT}/backups"
SOURCE_OBJECT_DIR="${TEMP_ROOT}/source-objects"
TARGET_OBJECT_DIR="${TEMP_ROOT}/target-objects"

export BIDSCOPE_POSTGRES_DB="bidscope"
export BIDSCOPE_POSTGRES_USER="bidscope"
export BIDSCOPE_POSTGRES_PASSWORD="bidscope"
export BIDSCOPE_DATABASE_URL="postgresql+asyncpg://bidscope:bidscope@postgres:5432/bidscope"
export BIDSCOPE_CHECKPOINT_DATABASE_URL="postgresql+psycopg://bidscope:bidscope@postgres:5432/bidscope"
export BIDSCOPE_REAL_MODEL_ENABLED="false"
export BIDSCOPE_ADMIN_TOKEN="recovery-admin-token-012345678901234567890123"
export BIDSCOPE_ALLOWED_ORIGINS='["http://127.0.0.1"]'
export BIDSCOPE_TRUSTED_HOSTS='["127.0.0.1"]'
export BIDSCOPE_EXTERNAL_SCHEME="http"
export BIDSCOPE_S3_ENDPOINT="http://minio:9000"
export BIDSCOPE_S3_REGION="us-east-1"
export BIDSCOPE_S3_BUCKET="bidscope-recovery"
export BIDSCOPE_S3_PREFIX="recovery"
export BIDSCOPE_S3_ACCESS_KEY="bidscope-recovery"
export BIDSCOPE_S3_SECRET_KEY="bidscope-recovery-secret"
export BIDSCOPE_BACKUP_S3_ENABLED="false"
export BIDSCOPE_TEST_CONTROL_TOKEN="recovery-${RUN_ID}-${RANDOM}"
export BIDSCOPE_RECOVERY_BACKUP_DIR="${BACKUP_DIR}"
export BIDSCOPE_RECOVERY_OBJECT_DIR="${SOURCE_OBJECT_DIR}"
export BIDSCOPE_RECOVERY_PORT="${BIDSCOPE_RECOVERY_PORT:-$((18000 + RANDOM % 1000))}"

base_url="http://127.0.0.1:${BIDSCOPE_RECOVERY_PORT}"
mkdir -p "${BACKUP_DIR}" "${SOURCE_OBJECT_DIR}" "${TARGET_OBJECT_DIR}"

json_field() {
  local json="$1"
  local field="$2"
  python3 -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "${json}" "${field}"
}

json_nested_field() {
  local json="$1"
  local path="$2"
  python3 -c 'from functools import reduce; import json, sys; print(reduce(lambda value, key: value[key], sys.argv[2].split("."), json.loads(sys.argv[1])))' "${json}" "${path}"
}

wait_ready() {
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS "${base_url}/readyz" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "API did not become ready at ${base_url}" >&2
  compose logs --tail=200 api
  return 1
}

wait_for_status() {
  local run_id="$1"
  local expected="$2"
  local attempt response status
  for attempt in $(seq 1 120); do
    response="$(curl -fsS "${base_url}/api/runs/${run_id}")"
    status="$(json_field "${response}" status)"
    if [[ "${status}" == "${expected}" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Run ${run_id} did not reach ${expected}" >&2
  return 1
}

assert_docx() {
  local run_id="$1"
  local path="${TEMP_ROOT}/${run_id}.docx"
  curl -fsS "${base_url}/api/reports/${run_id}/docx" -o "${path}"
  python3 - "${path}" <<'PY'
from pathlib import Path
import sys

if Path(sys.argv[1]).read_bytes()[:4] != b"PK\x03\x04":
    raise SystemExit("restored DOCX is not an OOXML archive")
PY
}

create_and_complete_scheduled_run() {
  local scheduled_request='每周一上午 9 点，汇总近 7 天四川和重庆与智算中心服务器有关的预算 500 万以上的招标信息。'
  local created run_id
  created="$(curl -fsS -X POST "${base_url}/api/runs" -H 'Content-Type: application/json' --data "$(python3 -c 'import json,sys; print(json.dumps({"user_request": sys.argv[1]}))' "${scheduled_request}")")"
  run_id="$(json_field "${created}" id)"
  wait_for_status "${run_id}" "awaiting_confirmation"
  curl -fsS -X POST "${base_url}/api/runs/${run_id}/confirm" -H 'Content-Type: application/json' --data '{"action":"approve"}' >/dev/null
  wait_for_status "${run_id}" "completed"
  printf '%s\n' "${run_id}"
}

current_step="compose-config"
compose config -q

# Source environment: initialize deterministic data, exercise the E2E flow,
# then archive the populated databases and local object root.
current_step="source-infrastructure"
compose up -d postgres minio minio-init
current_step="source-migrations"
compose run --rm --no-deps api alembic upgrade head
current_step="source-checkpoint-setup"
compose run --rm --no-deps api bidscope checkpoints setup
current_step="source-snapshot-import"
compose run --rm --no-deps api bidscope snapshots import data/demo/batch-1
current_step="source-api-start"
compose up -d api
current_step="source-readiness"
wait_ready

current_step="source-report-flow"
old_run_id="$(create_and_complete_scheduled_run)"
old_report="$(curl -fsS "${base_url}/api/reports/${old_run_id}")"
old_evidence_version="$(json_nested_field "${old_report}" 'items.0.provenance.source_version_id')"
assert_docx "${old_run_id}"
current_step="source-subscription-create"
curl -fsS -X POST "${base_url}/api/subscriptions" -H 'Content-Type: application/json' --data "$(python3 -c 'import json,sys; print(json.dumps({"run_id": sys.argv[1]}))' "${old_run_id}")" >/dev/null
current_step="source-scheduler-tick"
source_tick="$(curl -fsS -X POST "${base_url}/api/test-controls/run-scheduler-tick" -H "X-Test-Control-Token: ${BIDSCOPE_TEST_CONTROL_TOKEN}")"
[[ "$(json_field "${source_tick}" run_scheduler_tick)" == "ok" ]]
[[ "$(json_field "${source_tick}" ran)" -ge 1 ]]
[[ "$(json_field "${source_tick}" failed)" -eq 0 ]]

current_step="backup-create"
backup_json="$(compose_capture run --rm --no-deps api bidscope ops backup create --retention-class daily --json | tail -n 1)"
backup_id="$(json_field "${backup_json}" backup_id)"
backup_created_at="$(python3 - "${BACKUP_DIR}/${backup_id}/manifest.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["created_at"])
PY
)"
manifest_hash="$(sha256sum "${BACKUP_DIR}/${backup_id}/manifest.json" | awk '{print $1}')"
rpo_hours="$(python3 - "${backup_created_at}" <<'PY'
from datetime import datetime, timezone
import sys

created = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print((datetime.now(timezone.utc) - created).total_seconds() / 3600)
PY
)"

# Destroy this drill's named volumes only.  The archived backup and separate,
# empty target object root are bind mounts outside the Compose volume set.
restore_started="$(date +%s)"
current_step="recovery-volume-destruction"
compose down -v --remove-orphans
export BIDSCOPE_RECOVERY_OBJECT_DIR="${TARGET_OBJECT_DIR}"
current_step="target-infrastructure"
compose up -d postgres minio minio-init

current_step="backup-restore"
compose_capture run --rm --no-deps api bidscope ops backup restore \
  "/app/data/backups/${backup_id}" \
  --target-database-url "${BIDSCOPE_DATABASE_URL}" \
  --target-checkpoint-database-url "${BIDSCOPE_CHECKPOINT_DATABASE_URL}" \
  --target-object-root /app/data/objects \
  --confirm --json >/dev/null

current_step="target-api-start"
compose up -d api
current_step="target-readiness"
wait_ready
current_step="restored-report-validation"
restored_report="$(curl -fsS "${base_url}/api/reports/${old_run_id}")"
restored_evidence_version="$(json_nested_field "${restored_report}" 'items.0.provenance.source_version_id')"
[[ "${restored_evidence_version}" == "${old_evidence_version}" ]]
assert_docx "${old_run_id}"
current_step="restored-new-run-validation"
new_run_id="$(create_and_complete_scheduled_run)"
current_step="restored-subscription-create"
curl -fsS -X POST "${base_url}/api/subscriptions" -H 'Content-Type: application/json' --data "$(python3 -c 'import json,sys; print(json.dumps({"run_id": sys.argv[1]}))' "${new_run_id}")" >/dev/null
current_step="restored-scheduler-tick"
restored_tick="$(curl -fsS -X POST "${base_url}/api/test-controls/run-scheduler-tick" -H "X-Test-Control-Token: ${BIDSCOPE_TEST_CONTROL_TOKEN}")"
[[ "$(json_field "${restored_tick}" run_scheduler_tick)" == "ok" ]]
[[ "$(json_field "${restored_tick}" ran)" -ge 1 ]]
[[ "$(json_field "${restored_tick}" failed)" -eq 0 ]]
rto_seconds="$(( $(date +%s) - restore_started ))"

current_step="rpo-rto-threshold-gate"
if python3 - "${rpo_hours}" "${rto_seconds}" <<'PY'
import sys
rpo, rto = map(float, sys.argv[1:])
raise SystemExit(0 if rpo <= 24 and rto <= 14400 else 1)
PY
then
  emit_evidence true
else
  emit_evidence false "rpo-or-rto-threshold" 1
  echo "Recovery gate breached: rpo_hours > 24 or rto_seconds > 14400" >&2
  exit 1
fi
