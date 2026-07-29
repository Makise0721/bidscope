"""Contract coverage for the clean-host backup recovery drill."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_recovery_smoke_script_covers_backup_restore_release_gate() -> None:
    """The drill is self-contained, test-mode only, and emits measurable evidence."""
    script = (PROJECT_ROOT / "scripts" / "backup_restore_smoke.sh").read_text(
        encoding="utf-8"
    )

    for required in (
        'PROJECT_NAME="bidscope-recovery-${RUN_ID}"',
        "BIDSCOPE_TEST_CONTROL_TOKEN",
        "started_at=",
        "backup_created_at=",
        "bidscope ops backup create --retention-class daily --json",
        "bidscope ops backup restore",
        "--confirm",
        "/readyz",
        "/api/test-controls/run-scheduler-tick",
        '"manifest_hash"',
        '"old_report_evidence_version"',
        '"backup_age_seconds"',
        '"restore_duration_seconds"',
        '"rpo_hours"',
        '"rto_seconds"',
        '"passed"',
        "rpo_hours > 24",
        "rto_seconds > 14400",
        "down -v",
        "compose config -q",
    ):
        assert required in script
