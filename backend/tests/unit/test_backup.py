"""P1-C restore safety contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from bidscope.backup import BackupError, BackupService


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args: list[str], *, env: dict[str, str], timeout: int) -> None:
        self.calls.append((args, env))


class FakeDatabaseInspector:
    def __init__(self, *, has_migration_table: bool = False, revision: str = "r1") -> None:
        self.has_migration_table = has_migration_table
        self.revision = revision
        self.calls: list[str] = []

    def is_empty(self, database_url: str) -> bool:
        self.calls.append(database_url)
        return not self.has_migration_table

    def current_revision(self, database_url: str) -> str:
        self.calls.append(database_url)
        return self.revision


def _write_manifest(
    backup_dir: Path,
    *,
    dump_bytes: bytes = b"dump",
    object_bytes: bytes = b"object",
) -> None:
    backup_dir.mkdir(parents=True)
    (backup_dir / "application.dump").write_bytes(dump_bytes)
    object_archive = backup_dir / "objects" / "object-000001.bin"
    object_archive.parent.mkdir()
    object_archive.write_bytes(object_bytes)
    manifest: dict[str, Any] = {
        "backup_version": "p1-v1",
        "backup_id": backup_dir.name,
        "created_at": "2026-07-28T00:00:00Z",
        "app_version": "0.1.0",
        "git_commit": "abc123",
        "migration_revisions": {"application": "r1", "checkpoint": "r1"},
        "database_dumps": {"application": "application.dump", "checkpoint": "application.dump"},
        "database_dump_sha256": {
            "application": hashlib.sha256(dump_bytes).hexdigest(),
            "checkpoint": hashlib.sha256(dump_bytes).hexdigest(),
        },
        "objects": [
            {
                "key": "reports/report.docx",
                "archive_path": "objects/object-000001.bin",
                "size": len(object_bytes),
                "sha256": hashlib.sha256(object_bytes).hexdigest(),
            }
        ],
        "counts": {"databases": 1, "objects": 1},
        "retention_class": "daily",
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _service(
    tmp_path: Path,
    *,
    inspector: FakeDatabaseInspector | None = None,
    runner: FakeCommandRunner | None = None,
) -> tuple[BackupService, FakeCommandRunner, FakeDatabaseInspector]:
    resolved_runner = runner or FakeCommandRunner()
    resolved_inspector = inspector or FakeDatabaseInspector()
    return (
        BackupService(
            backup_root=tmp_path / "backups",
            command_runner=resolved_runner,
            database_inspector=resolved_inspector,
        ),
        resolved_runner,
        resolved_inspector,
    )


def test_restore_requires_explicit_confirmation_before_any_preflight_write(
    tmp_path: Path,
) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "backups" / "backup-1"
    _write_manifest(backup_dir)

    with pytest.raises(BackupError, match="confirm") as error:
        service.restore(
            backup_dir=backup_dir,
            target_database_url="postgresql://user:secret@db/app",
            target_checkpoint_database_url="postgresql://user:secret@db/app",
            target_object_root=tmp_path / "target-objects",
            confirmed=False,
        )

    assert error.value.code == "restore_confirmation_required"
    assert not runner.calls
    assert not inspector.calls


def test_restore_rejects_non_empty_target_object_root(
    tmp_path: Path,
) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "backups" / "backup-1"
    _write_manifest(backup_dir)
    target_root = tmp_path / "target-objects"
    target_root.mkdir()
    (target_root / "existing.bin").write_bytes(b"do not overwrite")

    with pytest.raises(BackupError, match="empty") as error:
        service.restore(
            backup_dir=backup_dir,
            target_database_url="postgresql://user:secret@db/app",
            target_checkpoint_database_url="postgresql://user:secret@db/app",
            target_object_root=target_root,
            confirmed=True,
        )

    assert error.value.code == "restore_target_object_root_not_empty"
    assert not runner.calls
    assert not inspector.calls


def test_restore_rejects_target_database_with_migration_table(tmp_path: Path) -> None:
    inspector = FakeDatabaseInspector(has_migration_table=True)
    service, runner, _ = _service(tmp_path, inspector=inspector)
    backup_dir = tmp_path / "backups" / "backup-1"
    _write_manifest(backup_dir)

    with pytest.raises(BackupError, match="empty") as error:
        service.restore(
            backup_dir=backup_dir,
            target_database_url="postgresql://user:secret@db/app",
            target_checkpoint_database_url="postgresql://user:secret@db/app",
            target_object_root=tmp_path / "target-objects",
            confirmed=True,
        )

    assert error.value.code == "restore_target_database_not_empty"
    assert not runner.calls


def test_restore_rejects_invalid_manifest_before_target_checks(tmp_path: Path) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "backups" / "backup-1"
    backup_dir.mkdir(parents=True)
    (backup_dir / "manifest.json").write_text("{}", encoding="utf-8")
    target_root = tmp_path / "target-objects"
    target_root.mkdir()
    (target_root / "existing.bin").write_bytes(b"untouched")

    with pytest.raises(BackupError) as error:
        service.restore(
            backup_dir=backup_dir,
            target_database_url="postgresql://user:secret@db/app",
            target_checkpoint_database_url="postgresql://user:secret@db/app",
            target_object_root=target_root,
            confirmed=True,
        )

    assert error.value.code == "backup_manifest_invalid"
    assert not runner.calls
    assert not inspector.calls
    assert (target_root / "existing.bin").read_bytes() == b"untouched"


def test_verify_rejects_backup_path_outside_root(tmp_path: Path) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "outside"
    _write_manifest(backup_dir)

    with pytest.raises(BackupError) as error:
        service.verify(backup_dir)

    assert error.value.code == "backup_path_outside_root"
    assert not runner.calls
    assert not inspector.calls


def test_restore_accepts_explicit_backup_path_outside_default_root(tmp_path: Path) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "outside"
    _write_manifest(backup_dir)

    result = service.restore(
        backup_dir=backup_dir,
        target_database_url="postgresql://user:secret@db/app",
        target_checkpoint_database_url="postgresql://user:secret@db/app",
        target_object_root=tmp_path / "target-objects",
        confirmed=True,
    )

    assert result["status"] == "restored"
    assert len(runner.calls) == 1
    assert inspector.calls


def test_restore_rejects_shared_database_when_checkpoint_revision_differs(
    tmp_path: Path,
) -> None:
    service, runner, inspector = _service(tmp_path, inspector=FakeDatabaseInspector(revision="r1"))
    backup_dir = tmp_path / "backups" / "backup-1"
    _write_manifest(backup_dir)
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["migration_revisions"]["checkpoint"] = "r2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError) as error:
        service.restore(
            backup_dir=backup_dir,
            target_database_url="postgresql://user:secret@db/app",
            target_checkpoint_database_url="postgresql://user:secret@db/app",
            target_object_root=tmp_path / "target-objects",
            confirmed=True,
        )

    assert error.value.code == "restore_migration_revision_mismatch"
    assert len(runner.calls) == 1


def test_restore_pg_restore_is_non_destructive_and_hash_verifies(tmp_path: Path) -> None:
    service, runner, inspector = _service(tmp_path)
    backup_dir = tmp_path / "backups" / "backup-1"
    _write_manifest(backup_dir)
    target_root = tmp_path / "target-objects"

    result = service.restore(
        backup_dir=backup_dir,
        target_database_url="postgresql://user:secret@db/app",
        target_checkpoint_database_url="postgresql://user:secret@db/app",
        target_object_root=target_root,
        confirmed=True,
    )

    assert result["status"] == "restored"
    assert result["backup_id"] == "backup-1"
    assert (target_root / "reports" / "report.docx").read_bytes() == b"object"
    assert len(runner.calls) == 1
    args, env = runner.calls[0]
    assert args[:2] == ["pg_restore", "--exit-on-error"]
    assert "--no-owner" in args
    assert not any(option in args for option in ("--clean", "--if-exists"))
    assert "secret" not in " ".join(args)
    assert env["PGPASSWORD"] == "secret"
    assert inspector.calls
