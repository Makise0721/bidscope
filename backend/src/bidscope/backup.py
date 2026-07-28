"""Verifiable local backup and non-destructive restore operations.

The restore path is deliberately conservative: it verifies the complete manifest,
checks that every destination is empty, and only then invokes ``pg_restore``
without overwrite flags. Database credentials are carried only in a subprocess
environment and never in command arguments or returned summaries.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

from pydantic import AwareDatetime, BaseModel, Field, ValidationError


class BackupError(Exception):
    """Bounded, machine-readable backup operation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message[:240]
        super().__init__(self.message)


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


class CommandRunner(Protocol):
    def __call__(self, args: list[str], *, env: dict[str, str], timeout: int) -> None: ...


class DatabaseInspector(Protocol):
    def is_empty(self, database_url: str) -> bool: ...
    def current_revision(self, database_url: str) -> str: ...


class _SubprocessRunner:
    def __call__(self, args: list[str], *, env: dict[str, str], timeout: int) -> None:
        try:
            subprocess.run(args, env=env, check=True, timeout=timeout, capture_output=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise BackupError("backup_tool_failed", "backup database tool failed") from error


class _PostgresInspector:
    """Small synchronous inspector used only at the backup boundary."""

    def _connect(self, database_url: str) -> Any:
        try:
            import psycopg

            return psycopg.connect(_driverless_dsn(database_url), connect_timeout=10)
        except Exception as error:
            raise BackupError(
                "backup_database_inspection_failed", "database inspection failed"
            ) from error

    def is_empty(self, database_url: str) -> bool:
        connection = self._connect(database_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.alembic_version')")
                return cursor.fetchone()[0] is None
        finally:
            connection.close()

    def current_revision(self, database_url: str) -> str:
        connection = self._connect(database_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                rows = cursor.fetchall()
            if len(rows) != 1 or not rows[0][0]:
                raise BackupError(
                    "backup_migration_revision_invalid", "expected one migration revision"
                )
            return str(rows[0][0])
        finally:
            connection.close()


def _driverless_dsn(database_url: str) -> str:
    """Return a psycopg-compatible URL without an async driver suffix."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupError("backup_file_unreadable", "backup file could not be read") from error
    return digest.hexdigest()


def safe_backup_path(path: Path, backup_root: Path) -> Path:
    """Resolve a backup directory and reject paths outside its configured root."""
    resolved = path.resolve()
    root = backup_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise BackupError("backup_path_outside_root", "backup path is outside the configured root")
    return resolved


def _safe_relative_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or "\\" in relative:
        raise BackupError("backup_manifest_invalid", "manifest path is not a safe relative path")
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BackupError("backup_manifest_invalid", "manifest path is not a safe relative path")
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents:
        raise BackupError("backup_manifest_invalid", "manifest path escapes backup directory")
    return resolved


def _safe_object_key(key: str) -> Path:
    if not key or key.startswith(("/", "\\")) or "\\" in key:
        raise BackupError("backup_manifest_invalid", "object key is not safe")
    parts = Path(key).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupError("backup_manifest_invalid", "object key is not safe")
    return Path(*parts)


def _read_manifest(backup_dir: Path, backup_root: Path) -> BackupManifest:
    resolved_dir = safe_backup_path(backup_dir, backup_root)
    manifest_path = _safe_relative_path(resolved_dir, "manifest.json")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return BackupManifest.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise BackupError("backup_manifest_invalid", "backup manifest is invalid") from error


def verify_manifest(backup_dir: Path, backup_root: Path | None = None) -> BackupManifest:
    """Parse and verify every dump/object hash before a restore can start."""
    root = backup_root or backup_dir.parent
    manifest = _read_manifest(backup_dir, root)
    resolved_dir = backup_dir.resolve()
    for role, dump_name in manifest.database_dumps.items():
        dump_path = _safe_relative_path(resolved_dir, dump_name)
        if (
            not dump_path.is_file()
            or sha256_file(dump_path) != manifest.database_dump_sha256.get(role)
        ):
            raise BackupError(
                "backup_manifest_hash_mismatch", "database dump hash verification failed"
            )
    for item in manifest.objects:
        archive_path = _safe_relative_path(resolved_dir, item.archive_path)
        if not archive_path.is_file() or archive_path.stat().st_size != item.size:
            raise BackupError("backup_manifest_hash_mismatch", "object size verification failed")
        if sha256_file(archive_path) != item.sha256:
            raise BackupError("backup_manifest_hash_mismatch", "object hash verification failed")
        _safe_object_key(item.key)
    return manifest


def _parse_database_url(database_url: str) -> dict[str, str]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg", "postgresql+psycopg"}:
        raise BackupError("restore_database_url_invalid", "target database URL is invalid")
    try:
        hostname = parsed.hostname
        port = parsed.port or 5432
    except ValueError as error:
        raise BackupError(
            "restore_database_url_invalid", "target database URL is invalid"
        ) from error
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    database = unquote(parsed.path.lstrip("/"))
    if not hostname or not username or not password or not database or not database_url.startswith(
        f"{parsed.scheme}://"
    ):
        raise BackupError("restore_database_url_invalid", "target database URL is invalid")
    return {
        "host": hostname,
        "port": str(port),
        "user": username,
        "password": password,
        "database": database,
    }


def _database_identity(parts: Mapping[str, str]) -> tuple[str, str, str]:
    return (parts["host"].casefold(), parts["port"], parts["database"])


def _command_database_env(parts: Mapping[str, str]) -> dict[str, str]:
    return {
        "PGHOST": parts["host"],
        "PGPORT": parts["port"],
        "PGUSER": parts["user"],
        "PGDATABASE": parts["database"],
        "PGPASSWORD": parts["password"],
    }


class BackupService:
    """Backup operations with injectable process and database boundaries."""

    def __init__(
        self,
        backup_root: str | Path = "data/backups",
        *,
        command_runner: CommandRunner | None = None,
        database_inspector: DatabaseInspector | None = None,
        object_store: Any = None,
        database_urls: Mapping[str, str] | None = None,
        app_version: str = "0.1.0",
        git_commit: str = "unknown",
        tool_timeout: int = 900,
    ) -> None:
        self.backup_root = Path(backup_root)
        self.command_runner = command_runner or _SubprocessRunner()
        self.database_inspector = database_inspector or _PostgresInspector()
        self.object_store = object_store
        self.database_urls = dict(database_urls or {})
        self.app_version = app_version
        self.git_commit = git_commit
        self.tool_timeout = tool_timeout

    def restore(
        self,
        *,
        backup_dir: Path,
        target_database_url: str,
        target_checkpoint_database_url: str,
        target_object_root: Path,
        confirmed: bool,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise BackupError("restore_confirmation_required", "restore requires --confirm")

        manifest = verify_manifest(backup_dir)
        target_app = _parse_database_url(target_database_url)
        target_checkpoint = _parse_database_url(target_checkpoint_database_url)
        target_root = Path(target_object_root)
        if target_root.exists() and any(target_root.iterdir()):
            raise BackupError(
                "restore_target_object_root_not_empty", "target object root must be empty"
            )

        target_databases: dict[tuple[str, str, str], tuple[dict[str, str], str]] = {}
        for role, parts in (("application", target_app), ("checkpoint", target_checkpoint)):
            identity = _database_identity(parts)
            target_databases.setdefault(identity, (parts, role))
        for parts, _role in target_databases.values():
            if not self.database_inspector.is_empty(_format_database_url(parts)):
                raise BackupError(
                    "restore_target_database_not_empty", "target database must be empty"
                )

        restored_targets: set[tuple[str, str, str]] = set()
        for identity, (parts, target_role) in target_databases.items():
            source_role = target_role
            dump_name = manifest.database_dumps.get(source_role)
            if dump_name is None:
                raise BackupError("backup_manifest_invalid", "manifest is missing a database dump")
            dump_path = _safe_relative_path(backup_dir.resolve(), dump_name)
            if identity in restored_targets:
                continue
            args = [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                f"--dbname={parts['database']}",
                str(dump_path),
            ]
            try:
                self.command_runner(
                    args, env=_command_database_env(parts), timeout=self.tool_timeout
                )
            except BackupError:
                raise
            except Exception as error:
                raise BackupError("restore_database_failed", "database restore failed") from error
            restored_targets.add(identity)

        target_root.mkdir(parents=True, exist_ok=True)
        for item in manifest.objects:
            archive_path = _safe_relative_path(backup_dir.resolve(), item.archive_path)
            target_key = _safe_object_key(item.key)
            destination = (target_root / target_key).resolve()
            if target_root.resolve() not in destination.parents:
                raise BackupError("backup_manifest_invalid", "object key escapes target root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive_path, destination)
            if (
                destination.stat().st_size != item.size
                or sha256_file(destination) != item.sha256
            ):
                raise BackupError(
                    "restore_object_hash_mismatch", "restored object hash verification failed"
                )

        revisions: dict[str, str] = {}
        for _identity, (parts, target_role) in target_databases.items():
            actual = self.database_inspector.current_revision(_format_database_url(parts))
            roles = (
                ("application", "checkpoint")
                if target_role == "application"
                and _database_identity(target_checkpoint) == _database_identity(target_app)
                else (target_role,)
            )
            for role in roles:
                expected = manifest.migration_revisions.get(role)
                if expected != actual:
                    raise BackupError(
                        "restore_migration_revision_mismatch",
                        "restored migration revision mismatch",
                    )
                revisions[role] = actual

        return {
            "status": "restored",
            "backup_id": manifest.backup_id,
            "object_count": len(manifest.objects),
            "database_count": len(target_databases),
            "migration_revisions": revisions,
        }

    def verify(self, backup_dir: Path) -> dict[str, Any]:
        manifest = verify_manifest(backup_dir, self.backup_root)
        return {
            "status": "verified",
            "backup_id": manifest.backup_id,
            "object_count": len(manifest.objects),
            "database_count": len(set(manifest.database_dumps.values())),
        }

    def list(self) -> list[dict[str, Any]]:
        if not self.backup_root.exists():
            return []
        results: list[dict[str, Any]] = []
        for candidate in sorted(self.backup_root.iterdir()):
            if not candidate.is_dir():
                continue
            try:
                manifest = verify_manifest(candidate, self.backup_root)
            except BackupError:
                results.append({"backup_id": candidate.name, "status": "invalid"})
            else:
                results.append(
                    {
                        "backup_id": manifest.backup_id,
                        "status": "verified",
                        "retention_class": manifest.retention_class,
                        "created_at": manifest.created_at.isoformat(),
                    }
                )
        return results[:1000]

    def prune(self) -> dict[str, Any]:
        """Remove verified backups outside the configured retention window."""
        entries: list[tuple[BackupManifest, Path]] = []
        invalid: list[Path] = []
        if not self.backup_root.exists():
            return {"status": "pruned", "deleted_count": 0}
        for candidate in self.backup_root.iterdir():
            if not candidate.is_dir():
                continue
            try:
                manifest = verify_manifest(candidate, self.backup_root)
            except BackupError:
                invalid.append(candidate)
                continue
            entries.append((manifest, candidate))
        if not entries:
            return {"status": "pruned", "deleted_count": 0}
        newest = max(entries, key=lambda item: item[0].created_at)[0].backup_id
        keep_ids: set[str] = set()
        for retention, limit in (("daily", 7), ("weekly", 4)):
            candidates = sorted(
                (item for item in entries if item[0].retention_class == retention),
                key=lambda item: item[0].created_at,
                reverse=True,
            )
            keep_ids.update(item[0].backup_id for item in candidates[:limit])
        keep_ids.add(newest)
        deleted = 0
        for manifest, directory in entries:
            if manifest.backup_id in keep_ids:
                continue
            shutil.rmtree(directory)
            deleted += 1
        return {"status": "pruned", "deleted_count": deleted}

    def create(self, retention_class: str = "daily") -> dict[str, Any]:
        retention = retention_class
        if retention not in {"daily", "weekly"}:
            raise BackupError(
                "backup_retention_class_invalid",
                "retention class must be daily or weekly",
            )
        if not self.database_urls:
            raise BackupError(
                "backup_configuration_missing",
                "backup database configuration is missing",
            )

        self.backup_root.mkdir(parents=True, exist_ok=True)
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        backup_dir = self.backup_root / backup_id
        backup_dir.mkdir()
        try:
            parsed: dict[str, dict[str, str]] = {
                role: _parse_database_url(url) for role, url in self.database_urls.items()
            }
            distinct: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
            for role, parts in parsed.items():
                distinct.setdefault(_database_identity(parts), (role, parts))

            database_dumps: dict[str, str] = {}
            dump_hashes: dict[str, str] = {}
            revisions: dict[str, str] = {}
            for index, (_identity, (first_role, parts)) in enumerate(distinct.items()):
                filename = "application.dump" if index == 0 else f"{first_role}.dump"
                dump_path = backup_dir / filename
                args = [
                    "pg_dump",
                    "--format=custom",
                    "--file",
                    str(dump_path),
                    parts["database"],
                ]
                self.command_runner(
                    args,
                    env=_command_database_env(parts),
                    timeout=self.tool_timeout,
                )
                if not dump_path.is_file():
                    raise BackupError("backup_dump_missing", "database dump was not created")
                digest = sha256_file(dump_path)
                revision = self.database_inspector.current_revision(
                    _format_database_url(parts)
                )
                roles = [
                    role
                    for role, role_parts in parsed.items()
                    if _database_identity(role_parts) == _identity
                ]
                for role in roles:
                    database_dumps[role] = filename
                    dump_hashes[role] = digest
                    revisions[role] = revision

            objects: list[BackupObject] = []
            if self.object_store is not None:
                archive_dir = backup_dir / "objects"
                archive_dir.mkdir()
                for index, key in enumerate(self.object_store.list_keys(), start=1):
                    _safe_object_key(key)
                    data = self.object_store.get_bytes(key)
                    archive_path = archive_dir / f"object-{index:06d}.bin"
                    archive_path.write_bytes(data)
                    objects.append(
                        BackupObject(
                            key=key,
                            archive_path=archive_path.relative_to(backup_dir).as_posix(),
                            size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                        )
                    )

            manifest = BackupManifest(
                backup_version="p1-v1",
                backup_id=backup_id,
                created_at=datetime.now(UTC),
                app_version=self.app_version,
                git_commit=self.git_commit,
                migration_revisions=revisions,
                database_dumps=database_dumps,
                database_dump_sha256=dump_hashes,
                objects=objects,
                counts={"databases": len(distinct), "objects": len(objects)},
                retention_class=cast(Literal["daily", "weekly"], retention),
            )
            manifest_path = backup_dir / "manifest.json"
            temporary_manifest = backup_dir / ".manifest.json.tmp"
            temporary_manifest.write_text(
                json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_manifest.replace(manifest_path)
            verify_manifest(backup_dir, self.backup_root)
            return {
                "status": "verified",
                "backup_id": backup_id,
                "path": str(backup_dir),
                "object_count": len(objects),
                "database_count": len(distinct),
            }
        except BackupError:
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise
        except Exception as error:
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise BackupError("backup_creation_failed", "backup creation failed") from error


def _format_database_url(parts: Mapping[str, str]) -> str:
    # Internal inspector input only. It is never included in an error or result.
    from urllib.parse import quote

    return (
        f"postgresql://{quote(parts['user'])}:{quote(parts['password'])}@"
        f"{parts['host']}:{parts['port']}/{quote(parts['database'])}"
    )


__all__ = [
    "BackupError",
    "BackupManifest",
    "BackupObject",
    "BackupService",
    "sha256_file",
    "safe_backup_path",
    "verify_manifest",
]
