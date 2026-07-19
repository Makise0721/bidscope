from datetime import UTC, datetime

import pytest
from bidscope.domain.enums import CaptureKind, ImportStatus, InboxEventType, RunStatus, SourceName
from bidscope.domain.intents import SearchIntent
from bidscope.domain.notices import Money
from bidscope.domain.reports import ReportClaim
from bidscope.domain.runs import SerializableError
from bidscope.domain.snapshots import SnapshotManifest
from pydantic import ValidationError


def valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "bundle_id": "ccgp-central-20260718",
        "source": SourceName.CCGP,
        "capture_kind": CaptureKind.CURATED_PUBLIC_EXCERPT,
        "source_urls": ["https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/example.htm"],
        "retrieved_at": datetime(2026, 7, 18, tzinfo=UTC),
        "retrieval_outcome": "waf_blocked_after_public_verification",
        "parser_version": "ccgp-v1",
        "files": {"detail.html": "a" * 64, "expected.json": "b" * 64},
    }


# --- enums -------------------------------------------------------------------


def test_all_capture_kinds_exist() -> None:
    assert CaptureKind.RAW_RESPONSE == "raw_response"
    assert CaptureKind.CURATED_PUBLIC_EXCERPT == "curated_public_excerpt"
    assert CaptureKind.SYNTHETIC_DEMO == "synthetic_demo"


def test_synthetic_demo_is_distinct_source() -> None:
    assert SourceName.SYNTHETIC_DEMO != SourceName.CCGP
    assert SourceName.SYNTHETIC_DEMO != SourceName.GGZY
    assert SourceName.SYNTHETIC_DEMO == "synthetic_demo"


def test_run_status_has_terminal_and_intermediate_values() -> None:
    assert RunStatus.COMPLETED == "completed"
    assert RunStatus.FAILED == "failed"
    assert RunStatus.AWAITING_CONFIRMATION == "awaiting_confirmation"


def test_import_status_covers_lifecycle() -> None:
    assert ImportStatus.PENDING == "pending"
    assert ImportStatus.SUCCESS == "success"
    assert ImportStatus.FAILED == "failed"


def test_inbox_event_types_exist() -> None:
    assert InboxEventType.NEW_NOTICE == "new_notice"
    assert InboxEventType.MATERIAL_CHANGE == "material_change"
    assert InboxEventType.RUN_FAILURE == "run_failure"


# --- SnapshotManifest --------------------------------------------------------


def test_manifest_rejects_non_https_source() -> None:
    data = valid_manifest() | {"source_urls": ["http://example.com"]}
    try:
        SnapshotManifest.model_validate(data)
    except ValidationError as error:
        assert "https" in str(error).lower()
    else:
        raise AssertionError("manifest accepted a non-HTTPS source")


def test_synthetic_demo_manifest_requires_example_invalid_host() -> None:
    data = valid_manifest() | {
        "capture_kind": CaptureKind.SYNTHETIC_DEMO,
        "source": SourceName.SYNTHETIC_DEMO,
        "source_urls": ["https://www.ccgp.gov.cn/something"],
    }
    try:
        SnapshotManifest.model_validate(data)
    except ValidationError as error:
        assert "example.invalid" in str(error)
    else:
        raise AssertionError("synthetic_demo accepted a non-example.invalid URL")


def test_synthetic_demo_manifest_accepts_example_invalid_host() -> None:
    data = valid_manifest() | {
        "bundle_id": "demo-batch-1",
        "capture_kind": CaptureKind.SYNTHETIC_DEMO,
        "source": SourceName.SYNTHETIC_DEMO,
        "source_urls": ["https://example.invalid/demo-001"],
    }
    manifest = SnapshotManifest.model_validate(data)
    assert manifest.capture_kind == CaptureKind.SYNTHETIC_DEMO
    assert manifest.source_urls[0].host == "example.invalid"


def test_manifest_rejects_naive_timestamp() -> None:
    data = valid_manifest() | {"retrieved_at": datetime(2026, 7, 18)}
    try:
        SnapshotManifest.model_validate(data)
    except ValidationError:
        pass
    else:
        raise AssertionError("manifest accepted a naive datetime")


def test_manifest_round_trips_through_serialization() -> None:
    manifest = SnapshotManifest.model_validate(valid_manifest())
    dumped = manifest.model_dump(mode="json")
    assert dumped["source"] == "ccgp"
    assert dumped["capture_kind"] == "curated_public_excerpt"
    assert dumped["source_urls"] == [
        "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/example.htm"
    ]
    restored = SnapshotManifest.model_validate(dumped)
    assert restored.bundle_id == manifest.bundle_id


# --- Money -------------------------------------------------------------------


def test_money_uses_integer_minor_units() -> None:
    money = Money(minor_units=500_000_00, currency="CNY", raw_text="500万元")
    assert money.minor_units == 500_000_00
    assert isinstance(money.minor_units, int)
    assert money.currency == "CNY"
    assert money.raw_text == "500万元"


def test_money_rejects_fractional_minor_units() -> None:
    with pytest.raises(ValidationError):
        Money(minor_units=500_000_50.5, currency="CNY")


def test_money_serializes_minor_units_as_integer() -> None:
    money = Money(minor_units=500_000_00, currency="CNY")
    dumped = money.model_dump(mode="json")
    assert dumped["minor_units"] == 500_000_00
    assert isinstance(dumped["minor_units"], int)


# --- SearchIntent ------------------------------------------------------------


def test_search_intent_rejects_inverted_dates() -> None:
    try:
        SearchIntent(
            topics=["服务器"],
            published_from=datetime(2026, 7, 18, tzinfo=UTC),
            published_to=datetime(2026, 7, 11, tzinfo=UTC),
        )
    except ValidationError as error:
        assert "published_from" in str(error) or "published_to" in str(error)
    else:
        raise AssertionError("SearchIntent accepted published_from after published_to")


def test_search_intent_rejects_inverted_budget() -> None:
    try:
        SearchIntent(
            topics=["服务器"],
            min_budget=Money(minor_units=1_000_000_00, currency="CNY"),
            max_budget=Money(minor_units=500_000_00, currency="CNY"),
        )
    except ValidationError as error:
        assert "min_budget" in str(error) or "max_budget" in str(error)
    else:
        raise AssertionError("SearchIntent accepted min_budget > max_budget")


def test_search_intent_accepts_valid_date_range_and_budget() -> None:
    intent = SearchIntent(
        topics=["智算中心", "服务器"],
        published_from=datetime(2026, 7, 11, tzinfo=UTC),
        published_to=datetime(2026, 7, 18, tzinfo=UTC),
        min_budget=Money(minor_units=5_000_000_00, currency="CNY"),
    )
    assert intent.published_from <= intent.published_to
    assert intent.min_budget is not None


def test_search_intent_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        SearchIntent(
            topics=["服务器"],
            published_from=datetime(2026, 7, 11),
            published_to=datetime(2026, 7, 18),
        )


# --- ReportClaim -------------------------------------------------------------


def test_report_claim_requires_at_least_one_citation() -> None:
    try:
        ReportClaim(text="预算 500 万以上", citation_ids=[])
    except ValidationError as error:
        assert "citation" in str(error).lower()
    else:
        raise AssertionError("ReportClaim accepted empty citation_ids")


def test_report_claim_holds_multiple_citations() -> None:
    claim = ReportClaim(
        text="四川智算中心招标",
        citation_ids=["ev-001", "ev-002"],
    )
    assert len(claim.citation_ids) == 2


# --- errors ------------------------------------------------------------------


def test_error_types_are_serializable() -> None:
    error = SerializableError(
        code="intent_invalid",
        message="bad dates",
        details={"field": "published_from"},
    )
    dumped = error.model_dump()
    assert dumped["code"] == "intent_invalid"
    assert dumped["message"] == "bad dates"
    assert dumped["details"] == {"field": "published_from"}


def test_error_union_round_trips_through_json() -> None:
    import json

    error = SerializableError(code="retrieval_empty", message="no matches")
    encoded = json.dumps(error.model_dump(mode="json"))
    decoded = SerializableError.model_validate_json(encoded)
    assert decoded.code == "retrieval_empty"
    assert decoded.message == "no matches"


def test_error_code_rejects_arbitrary_string() -> None:
    """SerializableError.code must be one of the bounded error codes."""
    with pytest.raises(ValidationError):
        SerializableError(code="anything_goes", message="x")


def test_normalized_notice_rejects_synthetic_kind_with_official_source() -> None:
    from bidscope.domain.notices import NormalizedNotice

    with pytest.raises(ValidationError):
        NormalizedNotice(
            source=SourceName.CCGP,
            external_id="demo-1",
            source_url="https://example.invalid/demo-1",
            capture_kind=CaptureKind.SYNTHETIC_DEMO,
            parser_version="v1",
        )


def test_normalized_notice_rejects_official_kind_with_synthetic_source() -> None:
    from bidscope.domain.notices import NormalizedNotice

    with pytest.raises(ValidationError):
        NormalizedNotice(
            source=SourceName.SYNTHETIC_DEMO,
            external_id="SC-2026",
            source_url="https://www.ccgp.gov.cn/a.htm",
            capture_kind=CaptureKind.CURATED_PUBLIC_EXCERPT,
            parser_version="v1",
        )


def test_normalized_notice_rejects_synthetic_kind_without_demo_prefix() -> None:
    from bidscope.domain.notices import NormalizedNotice

    with pytest.raises(ValidationError):
        NormalizedNotice(
            source=SourceName.SYNTHETIC_DEMO,
            external_id="SC-2026",
            source_url="https://example.invalid/demo-1",
            capture_kind=CaptureKind.SYNTHETIC_DEMO,
            parser_version="v1",
        )


def test_normalized_notice_rejects_naive_datetime() -> None:
    from bidscope.domain.notices import NormalizedNotice

    with pytest.raises(ValidationError):
        NormalizedNotice(
            source=SourceName.CCGP,
            external_id="x",
            source_url="https://www.ccgp.gov.cn/a.htm",
            capture_kind=CaptureKind.RAW_RESPONSE,
            parser_version="v1",
            publish_time=datetime(2026, 7, 18),
        )


def test_report_rejects_naive_datetime() -> None:
    from bidscope.domain.reports import Report

    with pytest.raises(ValidationError):
        Report(run_id="r", generated_at=datetime(2026, 7, 18), query_conditions={})


def test_run_event_rejects_naive_datetime() -> None:
    from bidscope.domain.runs import RunEvent

    with pytest.raises(ValidationError):
        RunEvent(node="n", event="e", status="s", timestamp=datetime(2026, 7, 18))
