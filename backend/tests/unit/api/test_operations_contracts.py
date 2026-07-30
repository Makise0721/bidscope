"""RED contract tests for Task 17 operational API review findings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bidscope.api.routes.evaluations import _evaluation_row
from bidscope.api.routes.inbox import list_inbox_events
from bidscope.api.routes.reports import _serialize_report
from bidscope.api.routes.runs import CreateRunBody, list_runs
from bidscope.api.routes.sources import (
    _acquisition_run_row,
    _acquisition_status_row,
    _safe_source_urls,
    _source_acquisition_is_active,
    _source_row,
)
from bidscope.api.routes.subscriptions import pause_subscription, resume_subscription
from bidscope.clock import FixedClock
from fastapi import HTTPException
from pydantic import ValidationError


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> list[object]:
        return self._rows


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


def _query_service(rows: list[object]) -> SimpleNamespace:
    session = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(rows)))
    return SimpleNamespace(
        session_factory=Mock(return_value=_SessionContext(session)),
    )


def _bundle(retrieved_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id="bundle-db-id",
        bundle_id="bundle-20260718",
        capture_kind="curated_public_excerpt",
        retrieved_at=retrieved_at,
        parser_version="ccgp-v1",
        source_urls=["https://www.ccgp.gov.cn/tender/20260718"],
        manifest={"files": {"detail.json": "a" * 64}},
    )


def _successful_import() -> SimpleNamespace:
    return SimpleNamespace(status="success", warnings={}, error=None)


def test_report_serializer_includes_items_for_run_history_reports() -> None:
    report = SimpleNamespace(
        id="report-1",
        run_id="run-1",
        export_key="reports/run-1",
        conditions={"region": "四川"},
        freshness_window="7d",
        completeness_warning=None,
        generated_at=datetime(2026, 7, 18, tzinfo=UTC),
        items=[
            SimpleNamespace(
                title="四川智算中心服务器采购招标公告",
                source="synthetic_demo",
                url="https://example.invalid/demo-001",
            ),
        ],
    )

    serialized = _serialize_report(report)  # type: ignore[arg-type]

    assert isinstance(serialized["items"], list)
    assert serialized["items"] == [
        {
            "title": "四川智算中心服务器采购招标公告",
            "source": "synthetic_demo",
            "url": "https://example.invalid/demo-001",
        },
    ]


@pytest.mark.asyncio
async def test_run_list_exposes_only_a_bounded_request_preview() -> None:
    long_request = "request " * 100
    row = SimpleNamespace(
        id="run-1",
        status="completed",
        user_request=long_request,
    )

    response = await list_runs(service=_query_service([row]), limit=50)

    item = response["items"][0]
    assert "user_request" not in item
    assert isinstance(item["request_preview"], str)
    assert len(item["request_preview"]) <= 240
    assert item["request_preview"] != long_request


@pytest.mark.parametrize(
    ("clock_value", "expected_status", "expected_age_days"),
    [
        (datetime(2026, 7, 18, tzinfo=UTC), "stale", 8),
        (datetime(2026, 7, 15, tzinfo=UTC), "valid", 5),
    ],
)
def test_source_row_status_uses_injected_clock_for_deterministic_result(
    clock_value: datetime,
    expected_status: str,
    expected_age_days: int,
) -> None:
    retrieved_at = datetime(2026, 7, 10, tzinfo=UTC)
    bundles = [_bundle(retrieved_at)]
    imports = {"bundle-db-id": _successful_import()}

    row = _source_row(
        "ccgp",
        bundles,
        imports,
        clock=FixedClock(clock_value),
    )

    assert row["status"] == expected_status
    assert row["latest_valid_bundle"]["age_days"] == expected_age_days


def test_source_row_marks_bundle_stale_at_seven_days_and_twenty_three_hours() -> None:
    retrieved_at = datetime(2026, 7, 10, tzinfo=UTC)
    row = _source_row(
        "ccgp",
        [_bundle(retrieved_at)],
        {"bundle-db-id": _successful_import()},
        clock=FixedClock(retrieved_at + timedelta(days=7, hours=23)),
    )

    assert row["status"] == "stale"


def test_source_row_includes_import_warning_code_in_validation_warnings() -> None:
    retrieved_at = datetime(2026, 7, 18, tzinfo=UTC)
    import_record = SimpleNamespace(
        status="success",
        warnings={"code": "parse_drift"},
        error=None,
    )

    row = _source_row("ccgp", [_bundle(retrieved_at)], {"bundle-db-id": import_record})

    assert "parse_drift" in row["validation_warnings"]


def test_source_row_strips_query_and_fragment_from_display_urls() -> None:
    bundle = _bundle(datetime(2026, 7, 30, tzinfo=UTC))
    bundle.source_urls = [
        "https://www.ccgp.gov.cn/tender/20260718?token=secret#details",
        "https://user:password@www.ccgp.gov.cn/private",
        "https://www.ccgp.gov.cn/tender/20260718",
    ]

    row = _source_row("ccgp", [bundle], {"bundle-db-id": _successful_import()})

    assert row["latest_valid_bundle"]["source_urls"] == [
        "https://www.ccgp.gov.cn/tender/20260718"
    ]


def test_api_observes_worker_state_from_persisted_acquisition_records() -> None:
    assert not _source_acquisition_is_active(None, None, configured=False)
    assert _source_acquisition_is_active(
        SimpleNamespace(source="ccgp"), None, configured=False
    )
    assert _source_acquisition_is_active(
        None, SimpleNamespace(source="ccgp"), configured=False
    )


def test_safe_source_urls_rejects_credentials_and_malformed_values() -> None:
    assert _safe_source_urls(
        [
            "https://www.ccgp.gov.cn/path?secret=value",
            "https://user:password@www.ccgp.gov.cn/path",
            "https://[invalid/path",
        ]
    ) == ["https://www.ccgp.gov.cn/path"]


def test_create_run_body_rejects_user_request_longer_than_4000_characters() -> None:
    with pytest.raises(ValidationError):
        CreateRunBody(user_request="x" * 4001)


def test_source_row_exposes_provenance_metadata_for_display() -> None:
    retrieved_at = datetime(2026, 7, 18, tzinfo=UTC)

    row = _source_row(
        "ccgp",
        [_bundle(retrieved_at)],
        {"bundle-db-id": _successful_import()},
    )

    latest = row["latest_valid_bundle"]
    assert latest["source_urls"] == ["https://www.ccgp.gov.cn/tender/20260718"]
    assert latest["retrieved_at"] == "2026-07-18T00:00:00+00:00"
    assert latest["age_days"] is not None
    assert latest["hash_prefix"] == "aaaaaaaa"


def test_acquisition_status_is_bounded_and_contains_no_source_payload_metadata() -> None:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    cursor = SimpleNamespace(
        source="ccgp",
        last_success_at=now - timedelta(minutes=15),
        consecutive_failures=2,
    )
    run = SimpleNamespace(
        status="rate_limited",
        finished_at=now - timedelta(minutes=1),
        retry_after_seconds=120,
        failure_code="rate_limited",
        request_count=3,
        record_count=12,
        new_bundle_count=4,
        imported_notice_count=9,
        response_object_key="raw/secret-response.json",
        response_sha256="a" * 64,
        cursor_before="secret-cursor",
        cursor_after="secret-cursor-2",
    )

    row = _acquisition_status_row(
        cursor,
        run,
        now=now,
        enabled=True,
        poll_seconds=600,
    )

    assert row == {
        "source": "ccgp",
        "status": "rate_limited",
        "last_success_at": "2026-07-30T11:45:00+00:00",
        "next_run_at": "2026-07-30T12:01:00+00:00",
        "lag_seconds": 900,
        "consecutive_failures": 2,
        "failure_code": "rate_limited",
        "counts": {
            "requests": 3,
            "records": 12,
            "new_bundles": 4,
            "imported_notices": 9,
        },
    }
    serialized = str(row)
    assert "response_object_key" not in serialized
    assert "response_sha256" not in serialized
    assert "cursor_before" not in serialized
    assert "secret" not in serialized


def test_acquisition_history_row_excludes_object_keys_and_cursor_values() -> None:
    run = SimpleNamespace(
        id="run-1",
        source="ccgp",
        status="failed",
        started_at=datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 30, 11, 1, tzinfo=UTC),
        request_count=1,
        record_count=0,
        new_bundle_count=0,
        imported_notice_count=0,
        failure_code="authorization_rejected",
        http_status=403,
        retry_after_seconds=None,
        response_object_key="raw/secret-response.json",
        cursor_before="secret-cursor",
        cursor_after=None,
    )

    row = _acquisition_run_row(run)

    assert row == {
        "id": "run-1",
        "source": "ccgp",
        "status": "failed",
        "started_at": "2026-07-30T11:00:00+00:00",
        "finished_at": "2026-07-30T11:01:00+00:00",
        "counts": {
            "requests": 1,
            "records": 0,
            "new_bundles": 0,
            "imported_notices": 0,
        },
        "failure_code": "authorization_rejected",
        "http_status": 403,
        "retry_after_seconds": None,
    }
    assert "secret" not in str(row)


def test_evaluation_dto_includes_status() -> None:
    row = SimpleNamespace(
        id="eval-1",
        dataset_version="retrieval-v1",
        model="deterministic-fake",
        status="passed",
        environment="test",
        pricing_snapshot={"pricing_snapshot_date": "2026-07-18"},
        metrics={},
    )

    dto = _evaluation_row(row)

    assert dto["status"] == "passed"


@pytest.mark.asyncio
async def test_inbox_dto_preserves_a_bounded_message() -> None:
    full_message = "A very detailed operational message. " * 20
    row = SimpleNamespace(
        id="inbox-1",
        event_type="new_notice",
        title="New notice",
        message=full_message,
        read=False,
    )

    response = await list_inbox_events(service=_query_service([row]), limit=50)

    item = response["items"][0]
    assert isinstance(item["message"], str)
    assert len(item["message"]) <= 240
    assert item["message"] != full_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "initial_status"),
    [(pause_subscription, "paused"), (resume_subscription, "active")],
)
async def test_subscription_routes_reject_invalid_state_transitions(
    handler: object,
    initial_status: str,
) -> None:
    subscription = SimpleNamespace(id="sub-1", status=initial_status)
    session = SimpleNamespace(
        get=AsyncMock(return_value=subscription),
        commit=AsyncMock(),
    )
    service = SimpleNamespace(
        session_factory=Mock(return_value=_SessionContext(session)),
    )

    with pytest.raises(HTTPException) as error:
        await handler("sub-1", service=service)  # type: ignore[operator]

    assert error.value.status_code == 409
    assert subscription.status == initial_status
    session.commit.assert_not_awaited()
