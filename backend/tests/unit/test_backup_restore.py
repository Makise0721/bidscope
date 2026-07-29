"""Executable contracts for the clean-host backup recovery drill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backup_restore_smoke.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_drill(
    tmp_path: Path,
    *,
    tick_responses: list[dict[str, int | str]] | None = None,
    fail_config: bool = False,
    fail_date: bool = False,
    fail_cleanup: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the shell drill against local command doubles, never Docker/network."""
    # Keep source inspection encoding-explicit on Windows; execution below is
    # the actual contract, not a string-matching substitute.
    assert SCRIPT_PATH.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    if os.name == "nt":
        pytest.skip("the recovery shell contract executes on POSIX CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required for the recovery-drill contract")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    evidence_path = tmp_path / "recovery-evidence.json"
    tick_payloads = tick_responses or [
        {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
        {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
    ]
    (state_dir / "ticks.json").write_text(json.dumps(tick_payloads), encoding="utf-8")

    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
args="$*"
if [[ "$args" == *" config "* ]]; then
  [[ "${FAKE_FAIL_CONFIG:-0}" == "1" ]] && exit 41
  exit 0
fi
if [[ "$args" == *"bidscope ops backup create"* ]]; then
  mkdir -p "${BIDSCOPE_RECOVERY_BACKUP_DIR}/backup-1"
  created_at="$(/bin/date -u +%Y-%m-%dT%H:%M:%S+00:00)"
  printf '{"created_at":"%s"}' "$created_at" > \
    "${BIDSCOPE_RECOVERY_BACKUP_DIR}/backup-1/manifest.json"
  printf '%s\\n' '{"backup_id":"backup-1"}'
fi
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
joined = " ".join(args)
state = Path(os.environ["FAKE_STATE_DIR"])

if "/readyz" in joined:
    raise SystemExit(0)
if "/docx" in joined:
    output = args[args.index("-o") + 1]
    Path(output).write_bytes(b"PK\\x03\\x04")
    raise SystemExit(0)
if "/api/reports/" in joined:
    print(json.dumps({"items": [{"provenance": {"source_version_id": "version-1"}}]}))
    raise SystemExit(0)
if "/api/test-controls/run-scheduler-tick" in joined:
    count_path = state / "tick-count"
    count = int(count_path.read_text() if count_path.exists() else "0")
    count_path.write_text(str(count + 1))
    payload = json.loads((state / "ticks.json").read_text())[count]
    if count == 0:
        (state / "persisted-subscription-advanced").write_text("yes")
    elif not (state / "restored-subscription-created").exists():
        payload = {"run_scheduler_tick": "ok", "ran": 0, "failed": 0}
    else:
        (state / "final-validation-reached").write_text("yes")
    print(json.dumps(payload))
    raise SystemExit(0)
if "/api/subscriptions" in joined:
    count_path = state / "subscription-count"
    count = int(count_path.read_text() if count_path.exists() else "0")
    count_path.write_text(str(count + 1))
    if count == 1:
        (state / "restored-subscription-created").write_text("yes")
    print("{}")
    raise SystemExit(0)
if "/confirm" in joined:
    print("{}")
    raise SystemExit(0)
if "/api/runs" in joined and "-X POST" in joined:
    count_path = state / "created-count"
    count = int(count_path.read_text() if count_path.exists() else "0")
    count_path.write_text(str(count + 1))
    print(json.dumps({"id": "old" if count == 0 else "new"}))
    raise SystemExit(0)
if "/api/runs/old" in joined:
    count_path = state / "old-poll-count"
    count = int(count_path.read_text() if count_path.exists() else "0")
    count_path.write_text(str(count + 1))
    print(json.dumps({"status": "awaiting_confirmation" if count == 0 else "completed"}))
    raise SystemExit(0)
if "/api/runs/new" in joined:
    count_path = state / "new-poll-count"
    count = int(count_path.read_text() if count_path.exists() else "0")
    count_path.write_text(str(count + 1))
    print(json.dumps({"status": "awaiting_confirmation" if count == 0 else "completed"}))
    raise SystemExit(0)
raise SystemExit(f"unexpected curl invocation: {joined}")
""",
    )
    _write_executable(
        fake_bin / "date",
        """#!/usr/bin/env bash
set -eu
if [[ "${FAKE_FAIL_DATE:-0}" == "1" && "$*" == *"+%Y"* ]]; then
  exit 73
fi
if [[ "$*" == *"+%s"* ]]; then
  count_file="${FAKE_STATE_DIR}/date-count"
  count=0
  [[ -f "$count_file" ]] && count="$(cat "$count_file")"
  count=$((count + 1))
  printf '%s' "$count" > "$count_file"
  if [[ "$count" == "1" ]]; then
    printf '1000\\n'
  elif [[ -f "${FAKE_STATE_DIR}/final-validation-reached" ]]; then
    printf '2000\\n'
  else
    printf '1001\\n'
  fi
else
  /bin/date "$@"
fi
""",
    )
    if fail_cleanup:
        _write_executable(
            fake_bin / "rm",
            """#!/usr/bin/env bash
exit 91
""",
        )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "TMPDIR": str(tmp_path),
            "FAKE_STATE_DIR": str(state_dir),
            "FAKE_FAIL_CONFIG": "1" if fail_config else "0",
            "FAKE_FAIL_DATE": "1" if fail_date else "0",
            "BIDSCOPE_RECOVERY_EVIDENCE_PATH": str(evidence_path),
        }
    )
    result = subprocess.run(
        [bash, str(SCRIPT_PATH)],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence_path


@pytest.mark.parametrize(
    "tick_responses",
    [
        [
            {"run_scheduler_tick": "ok", "ran": 0, "failed": 0},
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
        ],
        [
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 1},
        ],
        [
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 1},
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
        ],
        [
            {"run_scheduler_tick": "ok", "ran": 1, "failed": 0},
            {"run_scheduler_tick": "ok", "ran": 0, "failed": 0},
        ],
    ],
    ids=[
        "source-tick-did-not-run",
        "restored-tick-failed",
        "source-tick-failed",
        "restored-tick-did-not-run",
    ],
)
def test_recovery_drill_rejects_unsuccessful_scheduler_ticks(
    tmp_path: Path, tick_responses: list[dict[str, int | str]]
) -> None:
    """Both source and restored ticks need a real successful scheduler run."""
    result, evidence_path = _run_drill(tmp_path, tick_responses=tick_responses)

    assert result.returncode != 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["error"]


def test_recovery_drill_emits_failure_evidence_for_early_compose_error(tmp_path: Path) -> None:
    """A failed Compose config still produces an uploadable false artifact."""
    result, evidence_path = _run_drill(tmp_path, fail_config=True)

    assert result.returncode == 41
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["error"] == "compose-config"
    assert evidence["started_at"]


def test_recovery_drill_emits_failure_evidence_before_temporary_setup(tmp_path: Path) -> None:
    """A clock failure before ``mktemp`` still yields safe false evidence."""
    result, evidence_path = _run_drill(tmp_path, fail_date=True)

    assert result.returncode == 73
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["error"] == "initialize-timestamps"
    assert evidence["started_at"]


def test_recovery_drill_preserves_original_failure_when_cleanup_fails(
    tmp_path: Path,
) -> None:
    """Best-effort cleanup must not replace the failed Compose exit status."""
    result, evidence_path = _run_drill(
        tmp_path, fail_config=True, fail_cleanup=True
    )

    assert result.returncode == 41
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["exit_code"] == 41


def test_recovery_rto_includes_final_restore_validation(tmp_path: Path) -> None:
    """RTO ends after, not before, ready/report/DOCX/run/tick validation."""
    result, evidence_path = _run_drill(tmp_path)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["rto_seconds"] == 1000
    assert evidence["restore_duration_seconds"] == 1000
