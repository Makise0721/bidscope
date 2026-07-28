"""Bounded, redacted audit event persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from bidscope.persistence.models import AuditEvent

MAX_AUDIT_STRING_LENGTH = 1000
MAX_AUDIT_DEPTH = 2
MAX_AUDIT_COLLECTION_ITEMS = 32
_REDACTED = "[REDACTED]"
_TRUNCATED = "[TRUNCATED]"
_SECRET_KEY_PARTS = frozenset(
    {"token", "authorization", "cookie", "secret", "api_key", "password"}
)


class AuditEventType(StrEnum):
    """Finite event vocabulary used by the P1 audit trail."""

    RUN_CREATED = "run.created"
    RUN_CONFIRMED = "run.confirmed"
    RUN_RETRIED = "run.retried"
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_PAUSED = "subscription.paused"
    SUBSCRIPTION_RESUMED = "subscription.resumed"
    SCHEDULER_TICK_STARTED = "scheduler.tick.started"
    SCHEDULER_TICK_COMPLETED = "scheduler.tick.completed"
    SCHEDULER_TICK_FAILED = "scheduler.tick.failed"
    SNAPSHOT_IMPORT_SUCCEEDED = "snapshot_import.succeeded"
    SNAPSHOT_IMPORT_FAILED = "snapshot_import.failed"
    REPORT_VIEWED = "report.viewed"
    DOCX_VIEWED = "docx.viewed"
    DOCX_RETRIED = "docx.retried"


class AuditOutcome(StrEnum):
    """Finite result vocabulary for audit events."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Bounded request and business correlation fields for one event."""

    request_id: str | None = None
    method: str | None = None
    path: str | None = None
    run_id: str | None = None
    subscription_id: str | None = None
    report_id: str | None = None
    snapshot_import_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _bounded_text(self.request_id))
        object.__setattr__(self, "method", _bounded_text(self.method, upper=True))
        object.__setattr__(self, "path", _normalized_path(self.path))
        for field_name in (
            "run_id",
            "subscription_id",
            "report_id",
            "snapshot_import_id",
            "error_code",
        ):
            object.__setattr__(self, field_name, _bounded_text(getattr(self, field_name)))


def _bounded_text(value: Any, *, upper: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "")
    text = text[:MAX_AUDIT_STRING_LENGTH]
    return text.upper() if upper else text


def _normalized_path(path: str | None) -> str | None:
    if path is None:
        return None
    parsed = urlsplit(path)
    normalized = parsed.path or "/"
    return _bounded_text(normalized)


def _is_secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def redact_audit_value(value: Any, *, _depth: int = 0) -> Any:
    """Return JSON-safe, recursively bounded data with secret-like keys masked."""
    if _depth >= MAX_AUDIT_DEPTH and isinstance(value, (dict, list, tuple)):
        return _TRUNCATED
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAX_AUDIT_COLLECTION_ITEMS:
                break
            key = _bounded_text(raw_key) or ""
            result[key] = _REDACTED if _is_secret_key(key) else redact_audit_value(
                raw_value, _depth=_depth + 1
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact_audit_value(item, _depth=_depth + 1)
            for item in list(value)[:MAX_AUDIT_COLLECTION_ITEMS]
        ]
    if isinstance(value, str):
        return value[:MAX_AUDIT_STRING_LENGTH]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(value)


async def record_audit_event(
    session: AsyncSession,
    context: AuditContext,
    event_type: AuditEventType | str,
    outcome: AuditOutcome | str,
    details: Any = None,
) -> AuditEvent:
    """Add and flush an event using the caller's transaction; never commit."""
    resolved_event_type = AuditEventType(event_type)
    resolved_outcome = AuditOutcome(outcome)
    event = AuditEvent(
        event_type=resolved_event_type.value,
        outcome=resolved_outcome.value,
        request_id=context.request_id,
        method=context.method,
        path=context.path,
        run_id=context.run_id,
        subscription_id=context.subscription_id,
        report_id=context.report_id,
        snapshot_import_id=context.snapshot_import_id,
        error_code=context.error_code,
        details=redact_audit_value(details if details is not None else {}),
    )
    session.add(event)
    await session.flush()
    return event
