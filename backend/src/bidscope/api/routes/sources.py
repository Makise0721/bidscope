"""Bounded snapshot provenance read APIs for operational views."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.clock import Clock, SystemClock
from bidscope.ingestion.scheduler import bounded_retry_delay_seconds
from bidscope.persistence.models import (
    SnapshotBundle,
    SnapshotImport,
    SourceAcquisitionRun,
    SourceSyncCursor,
)
from bidscope.persistence.repositories import SourceAcquisitionRepository

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
_MAX_HISTORY_PAGE_SIZE = 50
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


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


def _safe_failure_code(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _FAILURE_CODE_PATTERN.fullmatch(value):
        return value
    return "source_failure"


def _count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _acquisition_counts(run: SourceAcquisitionRun | Any | None) -> dict[str, int]:
    return {
        "requests": _count(getattr(run, "request_count", 0)),
        "records": _count(getattr(run, "record_count", 0)),
        "new_bundles": _count(getattr(run, "new_bundle_count", 0)),
        "imported_notices": _count(getattr(run, "imported_notice_count", 0)),
    }


def _safe_source_urls(value: object) -> list[str]:
    """Return bounded HTTPS URLs without query, fragment, or user-info data."""
    if not isinstance(value, (list, tuple)):
        return []
    safe_urls: list[str] = []
    for raw in value[:20]:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = urlsplit(raw)
            if (
                parsed.scheme.casefold() != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 443}
            ):
                continue
            safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            continue
        if safe and safe not in safe_urls:
            safe_urls.append(safe)
    return safe_urls


def _acquisition_run_row(run: SourceAcquisitionRun | Any) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "source": str(run.source),
        "status": str(run.status),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "counts": _acquisition_counts(run),
        "failure_code": _safe_failure_code(run.failure_code),
        "http_status": run.http_status if isinstance(run.http_status, int) else None,
        "retry_after_seconds": (
            run.retry_after_seconds
            if isinstance(run.retry_after_seconds, int) and run.retry_after_seconds >= 0
            else None
        ),
    }


def _elapsed_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    if now.tzinfo is None and then.tzinfo is not None:
        now = now.replace(tzinfo=then.tzinfo)
    elif now.tzinfo is not None and then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    return max(0, int((now - then).total_seconds()))


def _acquisition_status_row(
    cursor: SourceSyncCursor | Any | None,
    run: SourceAcquisitionRun | Any | None,
    *,
    now: datetime,
    enabled: bool,
    poll_seconds: int,
) -> dict[str, Any]:
    source = str(getattr(cursor, "source", None) or getattr(run, "source", "ccgp"))
    last_success_at = getattr(cursor, "last_success_at", None)
    run_status = str(getattr(run, "status", "")) if run is not None else ""
    if not enabled:
        status = "disabled"
        next_run_at = None
    elif run_status == "rate_limited":
        status = "rate_limited"
        finished_at = getattr(run, "finished_at", None) or now
        delay = bounded_retry_delay_seconds(getattr(run, "retry_after_seconds", None))
        next_run_at = finished_at + timedelta(seconds=delay)
    elif run_status in {"failed", "quarantined"}:
        status = "failed"
        finished_at = getattr(run, "finished_at", None) or now
        next_run_at = finished_at + timedelta(seconds=max(1, poll_seconds))
    elif last_success_at is None:
        status = "failed"
        next_run_at = now
    else:
        lag_seconds = _elapsed_seconds(now, last_success_at) or 0
        status = "stale" if lag_seconds > STALE_AFTER_DAYS * 86_400 else "healthy"
        finished_at = getattr(run, "finished_at", None) if run is not None else None
        next_run_at = (
            (finished_at or now) + timedelta(seconds=max(1, poll_seconds))
            if run_status != "running"
            else None
        )

    return {
        "source": source,
        "status": status,
        "last_success_at": _iso(last_success_at),
        "next_run_at": _iso(next_run_at),
        "lag_seconds": _elapsed_seconds(now, last_success_at),
        "consecutive_failures": _count(getattr(cursor, "consecutive_failures", 0)),
        "failure_code": _safe_failure_code(getattr(run, "failure_code", None))
        if run is not None
        else None,
        "counts": _acquisition_counts(run),
    }


def _source_acquisition_is_active(
    cursor: SourceSyncCursor | Any | None,
    run: SourceAcquisitionRun | Any | None,
    *,
    configured: bool,
) -> bool:
    """Use persisted acquisition state so the API can observe the worker role."""
    return configured or cursor is not None or run is not None


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
            "source_urls": _safe_source_urls(latest.source_urls),
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


@router.get("/status")
async def list_source_status(
    service: RunService = Depends(_run_service),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    """List bounded acquisition freshness metadata without source payloads."""
    settings = getattr(service, "settings", None)
    configured = bool(
        getattr(settings, "live_ingestion_enabled", False)
        and getattr(settings, "process_role", None) == "ingestion"
    )
    poll_seconds = int(getattr(settings, "ccgp_poll_seconds", 3600))
    now = service.clock.now()
    async with service.session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        cursors = await repository.list_source_statuses(limit=limit)
        runs = await repository.list_acquisition_runs(limit=limit)

    cursor_by_source = {cursor.source: cursor for cursor in cursors}
    latest_run_by_source: dict[str, SourceAcquisitionRun] = {}
    for run in runs:
        latest_run_by_source.setdefault(run.source, run)
    sources = sorted({"ccgp", *cursor_by_source, *latest_run_by_source})[:limit]
    return {
        "items": [
            _acquisition_status_row(
                cursor_by_source.get(source),
                latest_run_by_source.get(source),
                now=now,
                enabled=_source_acquisition_is_active(
                    cursor_by_source.get(source),
                    latest_run_by_source.get(source),
                    configured=configured,
                ),
                poll_seconds=poll_seconds,
            )
            for source in sources
        ]
    }


@router.get("/acquisition-runs")
async def list_source_acquisition_runs(
    service: RunService = Depends(_run_service),
    source: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=_MAX_HISTORY_PAGE_SIZE),
) -> dict[str, Any]:
    """List paginated, payload-free acquisition history for operators."""
    offset = (page - 1) * page_size
    async with service.session_factory() as session:
        repository = SourceAcquisitionRepository(session)
        runs = await repository.list_acquisition_runs(
            source=source,
            limit=page_size + 1,
            offset=offset,
        )
    has_more = len(runs) > page_size
    return {
        "items": [_acquisition_run_row(run) for run in runs[:page_size]],
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
    }
