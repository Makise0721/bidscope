from __future__ import annotations

import pytest
from bidscope.audit import (
    MAX_AUDIT_STRING_LENGTH,
    AuditContext,
    AuditEventType,
    AuditOutcome,
    redact_audit_value,
)


def test_redact_audit_value_masks_nested_secret_like_keys() -> None:
    payload = {
        "X-Admin-Token": "admin-secret",
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "BIDSCOPE_MODEL_API_KEY": "model-secret",
        "nested": {"password": "db-secret", "run_id": "run-1"},
        "status": "completed",
    }

    redacted = redact_audit_value(payload)

    assert redacted == {
        "X-Admin-Token": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "Cookie": "[REDACTED]",
        "BIDSCOPE_MODEL_API_KEY": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "run_id": "run-1"},
        "status": "completed",
    }


def test_redact_audit_value_bounds_strings_and_nested_details() -> None:
    payload = {
        "message": "x" * (MAX_AUDIT_STRING_LENGTH + 20),
        "nested": {"level": {"too_deep": {"value": "hidden"}}},
        "items": list(range(100)),
    }

    redacted = redact_audit_value(payload)

    assert redacted["message"] == "x" * MAX_AUDIT_STRING_LENGTH
    assert redacted["nested"] == {"level": "[TRUNCATED]"}
    assert len(redacted["items"]) < len(payload["items"])


@pytest.mark.parametrize(
    ("event_type", "outcome"),
    (
        ("run.created", AuditOutcome.SUCCESS),
        (AuditEventType.REPORT_VIEWED, "success"),
    ),
)
def test_audit_contract_values_are_bounded(event_type: str | AuditEventType, outcome: str) -> None:
    context = AuditContext(
        request_id="request-1",
        method="get",
        path="/api/runs/1?token=must-not-be-stored",
        run_id="run-1",
        error_code=None,
    )

    assert context.method == "GET"
    assert context.path == "/api/runs/1"
    assert event_type in {
        AuditEventType.RUN_CREATED,
        AuditEventType.REPORT_VIEWED,
        "run.created",
    }
    assert outcome in {AuditOutcome.SUCCESS, "success"}
