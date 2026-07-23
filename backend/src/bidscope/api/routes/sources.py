"""Bounded snapshot provenance read APIs for operational views."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.clock import Clock, SystemClock
from bidscope.persistence.models import SnapshotBundle, SnapshotImport

router = APIRouter(
    prefix="/api/sources",
    tags=["sources"],
    dependencies=[Depends(require_admin_token)],
)

# A source older than this window is still usable for audit, but is surfaced as
# stale so operators do not mistake it for current coverage.
STALE_AFTER_DAYS = 7
_MAX_WARNING_COUNT = 20
_WARNING_TEXT_LIMIT = 100
_DIAGNOSTIC_FIELDS = frozenset({"code", "message"})


def _run_service(request: Request) -> RunService:
    return cast(RunService, request.app.state.run_service)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bundle_hash_prefix(bundle: SnapshotBundle) -> str | None:
    """Return a short manifest hash without exposing manifest contents."""
    manifest = bundle.manifest or {}
    content_hash = manifest.get("content_hash")
    if isinstance(content_hash, str) and content_hash:
        return content_hash[:8]
    files = manifest.get("files")
    if isinstance(files, dict):
        for name in sorted(files):
            value = files[name]
            if isinstance(value, str) and value:
                return value[:8]
    return None


def _diagnostic_values(payload: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and key.startswith("snapshot_"):
                values.add(key[:_WARNING_TEXT_LIMIT])
            if key in _DIAGNOSTIC_FIELDS and isinstance(value, str) and value:
                values.add(value[:_WARNING_TEXT_LIMIT])
            values.update(_diagnostic_values(value))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            values.update(_diagnostic_values(value))
    elif isinstance(payload, str) and payload.startswith("snapshot_"):
        values.add(payload[:_WARNING_TEXT_LIMIT])
    return values


def _warnings(import_record: SnapshotImport | None) -> set[str]:
    if import_record is None:
        return {"snapshot_integrity_error"}
    warnings: set[str] = set()
    if import_record.status in {"failed", "invalid"}:
        warnings.add(
            "snapshot_integrity_error"
            if import_record.status == "invalid"
            else "snapshot_import_failed"
        )
    for payload in (import_record.warnings, import_record.error):
        warnings.update(_diagnostic_values(payload))
    return warnings


def _latest_imports(imports: list[SnapshotImport]) -> dict[str, SnapshotImport]:
    latest: dict[str, SnapshotImport] = {}
    for record in imports:
        key = str(record.snapshot_bundle_id)
        previous = latest.get(key)
        previous_time = previous.finished_at if previous else None
        current_time = record.finished_at or record.started_at
        if previous is None or current_time > (previous_time or previous.started_at):
            latest[key] = record
    return latest


def _source_row(
    source: str,
    bundles: list[SnapshotBundle],
    imports_by_bundle: dict[str, SnapshotImport],
    clock: Clock | None = None,
) -> dict[str, Any]:
    warnings: set[str] = set()
    valid_bundles: list[SnapshotBundle] = []
    for bundle in bundles:
        import_record = imports_by_bundle.get(str(bundle.id))
        warnings.update(_warnings(import_record))
        if import_record is not None and import_record.status == "success":
            valid_bundles.append(bundle)

    latest = max(
        valid_bundles,
        key=lambda bundle: (
            bundle.retrieved_at or datetime.min.replace(tzinfo=UTC),
            bundle.bundle_id,
        ),
        default=None,
    )
    latest_dto: dict[str, Any] | None = None
    status = "invalid"
    if latest is not None:
        retrieved_at = latest.retrieved_at
        reference_time = (clock or SystemClock()).now()
        age_delta = reference_time - retrieved_at if retrieved_at is not None else None
        age_days = max(0, age_delta.days) if age_delta is not None else None
        status = (
            "stale"
            if age_delta is not None and age_delta > timedelta(days=STALE_AFTER_DAYS)
            else "valid"
        )
        if status == "stale":
            warnings.add("snapshot_stale")
        hash_prefix = _bundle_hash_prefix(latest)
        latest_dto = {
            "bundle_id": latest.bundle_id,
            "file_identity": latest.bundle_id,
            "capture_kind": latest.capture_kind,
            "source_urls": list(latest.source_urls or [])[:20],
            "retrieved_at": _iso(retrieved_at),
            "hash_prefix": hash_prefix,
            "parser_version": latest.parser_version,
            "age_days": age_days,
        }
    else:
        warnings.add("snapshot_integrity_error")

    return {
        "source": source,
        "status": status,
        "latest_valid_bundle": latest_dto,
        "validation_warnings": sorted(warnings)[:_MAX_WARNING_COUNT],
    }


@router.get("")
async def list_sources(
    service: RunService = Depends(_run_service),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    """List source health and provenance metadata, never payload content."""
    async with service.session_factory() as session:
        source_result = await session.execute(
            sa.select(SnapshotBundle.source).distinct().order_by(SnapshotBundle.source)
        )
        source_names = {
            "ccgp",
            "ggzy",
            "synthetic_demo",
            *(str(source) for source in source_result.scalars()),
        }
        source_names = set(sorted(source_names)[:limit])
        grouped: dict[str, list[SnapshotBundle]] = {}
        imports: list[SnapshotImport] = []
        for source in sorted(source_names):
            bundle_result = await session.execute(
                sa.select(SnapshotBundle)
                .where(SnapshotBundle.source == source)
                .order_by(
                    SnapshotBundle.retrieved_at.desc().nullslast(),
                    SnapshotBundle.bundle_id,
                )
                .limit(100)
            )
            source_bundles = list(bundle_result.scalars())
            grouped[source] = source_bundles
            bundle_ids = [bundle.id for bundle in source_bundles]
            if bundle_ids:
                import_result = await session.execute(
                    sa.select(SnapshotImport)
                    .where(SnapshotImport.snapshot_bundle_id.in_(bundle_ids))
                    .order_by(SnapshotImport.started_at.desc(), SnapshotImport.id)
                )
                imports.extend(import_result.scalars())

    latest_imports = _latest_imports(imports)
    import_by_bundle = {str(bundle_id): record for bundle_id, record in latest_imports.items()}
    items = [
        _source_row(source, grouped[source], import_by_bundle, clock=service.clock)
        for source in sorted(source_names)
    ]
    return {"items": items}
