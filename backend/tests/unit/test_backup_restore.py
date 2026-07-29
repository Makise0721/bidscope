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
    fail_backup_cli: bool = False,
    missing_backup_manifest: bool = False,
    fail_manifest_hash: bool = False,
    pretty_backup_json: bool = False,
    python_command: str | None = None,
    large_report: bool = False,
    recovery_temp_root: Path | None = None,
    uname_output: str | None = None,
    cygpath_output: Path | None = None,
    require_msys_no_pathconv: bool = False,
    require_writable_mounts: bool = False,
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
    if python_command == "py -3":
        _write_executable(
            fake_bin / "py",
            """#!/usr/bin/env bash
[[ "${1:-}" == "-3" ]] && shift
for argument in "$@"; do
  [[ "${#argument}" -le 4096 ]] || exit 74
done
exec python3 "$@"
""",
        )
    if uname_output is not None:
        _write_executable(
            fake_bin / "uname",
            f"""#!/usr/bin/env bash
printf '%s\\n' {uname_output!r}
""",
        )
    if cygpath_output is not None:
        cygpath_output.mkdir()
        _write_executable(
            fake_bin / "cygpath",
            f"""#!/usr/bin/env bash
printf '%s\\n' {str(cygpath_output)!r}
""",
        )

    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
args="$*"
for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "-p" ]]; then
    next_index=$((index + 1))
    project_name="${!next_index}"
    [[ "$project_name" =~ ^[a-z0-9][a-z0-9_.-]*$ ]] || exit 52
    break
  fi
done
if [[ "$args" == *" config "* ]]; then
  [[ "${FAKE_FAIL_CONFIG:-0}" == "1" ]] && exit 41
  exit 0
fi
if [[ "$args" == *"bidscope snapshots import"* \
  && "${FAKE_REQUIRE_WRITABLE_MOUNTS:-0}" == "1" ]]; then
  for directory in "${BIDSCOPE_RECOVERY_BACKUP_DIR}" "${BIDSCOPE_RECOVERY_OBJECT_DIR}"; do
    [[ "$(stat -c '%a' "${directory}")" == "777" ]] || exit 75
  done
fi
if [[ "$args" == *"bidscope ops backup create"* ]]; then
  if [[ "${FAKE_FAIL_BACKUP_CLI:-0}" == "1" ]]; then
    printf '%s\\n' '{"code":"backup_tool_failed"}'
    exit 67
  fi
  printf '%s' "${BIDSCOPE_RECOVERY_BACKUP_DIR}" > "${FAKE_STATE_DIR}/backup-root"
  mkdir -p "${BIDSCOPE_RECOVERY_BACKUP_DIR}/backup-1"
  if [[ "${FAKE_MISSING_BACKUP_MANIFEST:-0}" == "1" ]]; then
    printf '%s\\n' '{"backup_id":"backup-1"}'
    exit 0
  fi
  created_at="$(/bin/date -u +%Y-%m-%dT%H:%M:%S+00:00)"
  printf '{"created_at":"%s"}' "$created_at" > \
    "${BIDSCOPE_RECOVERY_BACKUP_DIR}/backup-1/manifest.json"
  if [[ "${FAKE_PRETTY_BACKUP_JSON:-0}" == "1" ]]; then
    printf '%s\\n' '{' '  "backup_id": "backup-1"' '}'
  else
    printf '%s\\n' '{"backup_id":"backup-1"}'
  fi
fi
if [[ "$args" == *"bidscope ops backup restore"* ]]; then
  [[ "${FAKE_REQUIRE_MSYS_NO_PATHCONV:-0}" != "1" || "${MSYS_NO_PATHCONV:-}" == "1" ]] || exit 68
  if [[ "${FAKE_REQUIRE_WRITABLE_MOUNTS:-0}" == "1" ]]; then
    for directory in "${BIDSCOPE_RECOVERY_BACKUP_DIR}" "${BIDSCOPE_RECOVERY_OBJECT_DIR}"; do
      [[ "$(stat -c '%a' "${directory}")" == "777" ]] || exit 75
    done
  fi
  printf '%s' "$args" > "${FAKE_STATE_DIR}/restore-command"
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
    source_query = (state / "source-query").read_text(encoding="utf-8")
    if "近 7 天" in source_query:
        print(json.dumps({"items": []}))
    else:
        report = {"items": [{"provenance": {"source_version_id": "version-1"}}]}
        if os.environ["FAKE_LARGE_REPORT"] == "1":
            report["items"][0]["padding"] = "x" * 20000
        print(json.dumps(report))
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
    request_body = json.loads(args[args.index("--data") + 1])
    if count == 0:
        (state / "source-query").write_text(request_body["user_request"], encoding="utf-8")
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
    if fail_manifest_hash:
        _write_executable(
            fake_bin / "sha256sum",
            """#!/usr/bin/env bash
exit 69
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
            "FAKE_FAIL_BACKUP_CLI": "1" if fail_backup_cli else "0",
            "FAKE_MISSING_BACKUP_MANIFEST": "1" if missing_backup_manifest else "0",
            "FAKE_PRETTY_BACKUP_JSON": "1" if pretty_backup_json else "0",
            "FAKE_REQUIRE_MSYS_NO_PATHCONV": "1" if require_msys_no_pathconv else "0",
            "FAKE_REQUIRE_WRITABLE_MOUNTS": "1" if require_writable_mounts else "0",
            "BIDSCOPE_RECOVERY_EVIDENCE_PATH": str(evidence_path),
            "BIDSCOPE_PYTHON_COMMAND": python_command or "python3",
            "FAKE_LARGE_REPORT": "1" if large_report else "0",
        }
    )
    if recovery_temp_root is not None:
        recovery_temp_root.mkdir()
        environment["BIDSCOPE_RECOVERY_TEMP_ROOT"] = str(recovery_temp_root)
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


def test_recovery_drill_supports_a_multi_token_python_launcher(tmp_path: Path) -> None:
    """A command array preserves arguments and stdin for failure evidence."""
    result, evidence_path = _run_drill(
        tmp_path, fail_config=True, python_command="py -3"
    )

    assert result.returncode == 41
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["error"] == "compose-config"


def test_recovery_drill_uses_a_batch_matchable_scheduled_query(tmp_path: Path) -> None:
    """A moving seven-day filter must not empty the committed batch-1 report."""
    result, evidence_path = _run_drill(tmp_path)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["old_report_evidence_version"] == "version-1"


def test_recovery_drill_prepares_writable_bind_mounts_for_non_root_api(tmp_path: Path) -> None:
    """The non-root image user must be able to write each drill-owned mount."""
    result, evidence_path = _run_drill(tmp_path, require_writable_mounts=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["passed"] is True


def test_recovery_drill_traverses_report_list_indices_with_py_launcher(
    tmp_path: Path,
) -> None:
    """The evidence path resolves the first item from a nonempty report list."""
    result, evidence_path = _run_drill(tmp_path, python_command="py -3")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["old_report_evidence_version"] == "version-1"


def test_recovery_drill_parses_large_reports_via_python_stdin(tmp_path: Path) -> None:
    """The multi-token launcher rejects a report passed as one huge argument."""
    result, evidence_path = _run_drill(
        tmp_path, python_command="py -3", large_report=True
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["old_report_evidence_version"] == "version-1"


def test_recovery_drill_uses_one_windows_host_path_for_backup_and_manifest(
    tmp_path: Path,
) -> None:
    """MSYS conversion supplies one host path to Compose and shell checks."""
    windows_host_root = tmp_path / "windows-host-root"
    result, evidence_path = _run_drill(
        tmp_path,
        uname_output="MSYS_NT-10.0",
        cygpath_output=windows_host_root,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert (tmp_path / "state" / "backup-root").read_text(encoding="utf-8").startswith(
        str(windows_host_root)
    )


def test_recovery_drill_preserves_container_restore_path_on_msys(tmp_path: Path) -> None:
    """MSYS must not rewrite /app paths passed to docker.exe."""
    windows_host_root = tmp_path / "windows-host-root"
    result, evidence_path = _run_drill(
        tmp_path,
        uname_output="MSYS_NT-10.0",
        cygpath_output=windows_host_root,
        require_msys_no_pathconv=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["passed"] is True
    assert "/app/data/backups/backup-1" in (
        tmp_path / "state" / "restore-command"
    ).read_text(encoding="utf-8")


def test_recovery_drill_parses_pretty_printed_backup_cli_json(tmp_path: Path) -> None:
    """The backup CLI deliberately formats --json output across lines."""
    result, evidence_path = _run_drill(tmp_path, pretty_backup_json=True)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["backup_id"] == "backup-1"


@pytest.mark.parametrize(
    ("kwargs", "expected_stage", "expected_exit"),
    [
        ({"fail_backup_cli": True}, "backup-cli", 67),
        ({"missing_backup_manifest": True}, "backup-manifest-read", 1),
        ({"fail_manifest_hash": True}, "backup-manifest-hash", 69),
    ],
)
def test_recovery_drill_reports_precise_backup_failure_stage(
    tmp_path: Path,
    kwargs: dict[str, bool],
    expected_stage: str,
    expected_exit: int,
) -> None:
    """Backup evidence distinguishes CLI, manifest, and digest failures."""
    result, evidence_path = _run_drill(tmp_path, **kwargs)

    assert result.returncode == expected_exit
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is False
    assert evidence["error"] == expected_stage


def test_recovery_rto_includes_final_restore_validation(tmp_path: Path) -> None:
    """RTO ends after, not before, ready/report/DOCX/run/tick validation."""
    result, evidence_path = _run_drill(tmp_path)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["rto_seconds"] == 1000
    assert evidence["restore_duration_seconds"] == 1000
