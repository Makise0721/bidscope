from __future__ import annotations

import json
import logging

import pytest
from bidscope.config import Settings
from bidscope.main import create_app
from bidscope.observability import (
    JsonFormatter,
    MetricsRegistry,
    RequestContext,
    get_request_context,
    valid_request_id,
)
from fastapi.testclient import TestClient


def test_valid_request_id_is_bounded_and_rejects_untrusted_values() -> None:
    assert valid_request_id("request-123._~")
    assert not valid_request_id("")
    assert not valid_request_id("bad request")
    assert not valid_request_id("bad\\nrequest")
    assert not valid_request_id("x" * 129)


def test_request_context_round_trips_through_healthz_and_is_cleared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = TestClient(create_app(Settings(app_mode="demo")))

    response = client.get("/healthz", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-123"
    assert get_request_context() is None
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    request_record = next(record for record in records if record.get("event") == "http_request")
    assert request_record["request_id"] == "request-123"
    assert request_record["method"] == "GET"
    assert request_record["normalized_path"] == "/healthz"
    assert request_record["status"] == 200
    assert isinstance(request_record["duration_ms"], (int, float))
    assert request_record["exception_type"] is None


def test_invalid_request_id_is_replaced_with_uuid() -> None:
    client = TestClient(create_app(Settings(app_mode="demo")))

    response = client.get("/healthz", headers={"X-Request-ID": "bad\nrequest"})

    request_id = response.headers["x-request-id"]
    assert request_id != "bad\nrequest"
    assert len(request_id) == 36
    assert request_id.count("-") == 4


def test_json_formatter_redacts_sensitive_fields_and_bounds_nested_values() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.structured = {
        "event": "test",
        "authorization": "Bearer top-secret-token",
        "safe": "x" * 1001,
        "nested": {"level": {"too_deep": "report body"}},
    }

    payload = json.loads(formatter.format(record))

    assert payload["authorization"] == "[REDACTED]"
    assert len(payload["safe"]) == 1000
    assert payload["nested"]["level"] == "[TRUNCATED]"
    assert "top-secret-token" not in formatter.format(record)
    assert "report body" not in formatter.format(record)


def test_metrics_registry_only_accepts_known_names_and_bounded_labels() -> None:
    registry = MetricsRegistry()

    registry.counter("bidscope_http_requests_total", {"status": "2xx"})
    with pytest.raises(ValueError):
        registry.counter("bidscope_unknown_total", {})
    with pytest.raises(ValueError):
        registry.counter(
            "bidscope_http_requests_total",
            {"status": "2xx", "request_id": "request-123"},
        )
    with pytest.raises(ValueError):
        registry.counter("bidscope_http_requests_total", {"status": "/api/runs/123"})


def test_snapshot_import_metrics_accept_canonical_synthetic_demo_source() -> None:
    """The fixed source vocabulary matches the snapshot manifest enum."""
    registry = MetricsRegistry()

    registry.counter(
        "bidscope_snapshot_imports_total",
        {"source": "synthetic_demo", "outcome": "success"},
    )
    with pytest.raises(ValueError):
        registry.counter(
            "bidscope_snapshot_imports_total",
            {"source": "demo", "outcome": "success"},
        )


def test_metrics_registry_renders_prometheus_histograms_without_arbitrary_ids() -> None:
    registry = MetricsRegistry()

    registry.observe(
        "bidscope_http_request_duration_seconds",
        0.25,
        {"status": "2xx"},
    )

    rendered = registry.render_prometheus()

    assert "# TYPE bidscope_http_request_duration_seconds histogram" in rendered
    assert 'bidscope_http_request_duration_seconds_bucket{status="2xx",le="0.5"}' in rendered
    assert 'bidscope_http_request_duration_seconds_count{status="2xx"} 1' in rendered
    assert 'bidscope_http_request_duration_seconds_sum{status="2xx"} 0.25' in rendered
    assert "request-123" not in rendered
    assert "token" not in rendered.lower()


def test_metrics_endpoint_requires_admin_token_outside_demo_mode() -> None:
    settings = Settings(
        app_mode="production",
        admin_token="production-admin-token-sentinel-0123456789",
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope-test",
        s3_access_key="test-access-key-sentinel",
        s3_secret_key="test-secret-key-sentinel",
        allowed_origins=["https://console.example.test"],
        trusted_hosts=["testserver"],
        external_scheme="https",
        database_url=(
            "postgresql+asyncpg://bidscope:database-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
        checkpoint_database_url=(
            "postgresql+psycopg://bidscope:checkpoint-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
    )
    client = TestClient(create_app(settings), base_url="https://testserver")

    assert client.get("/metrics").status_code == 401
    response = client.get(
        "/metrics",
        headers={"X-Admin-Token": "production-admin-token-sentinel-0123456789"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_request_context_is_a_small_immutable_value() -> None:
    context = RequestContext(request_id="request-123", method="GET", normalized_path="/healthz")

    assert context.request_id == "request-123"
    with pytest.raises(AttributeError):
        context.request_id = "other"  # type: ignore[misc]
