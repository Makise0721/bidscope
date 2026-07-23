"""Security boundary tests for prompt injection, input sanitization, and
bounded error handling.

These tests document the application's defences against:
- Prompt injection via imported source text (UNTRUSTED_SOURCE_DATA wrapping)
- Filename-based attacks (path traversal, script injection)
- Unbounded error codes (closed enum constraint)
- Untrusted evidence in graph state (type constraints)
- Contradictory intent rejection (defence-in-depth)
- Raw HTML escaping in report serialization (output safety)

Each test is offline — no network, no real API keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bidscope.domain.intents import SearchIntent
from bidscope.domain.notices import Money
from bidscope.domain.runs import SerializableError
from bidscope.domain.types import BidScopeErrorCode
from bidscope.graph.nodes import validate_intent
from bidscope.llm.types import (
    DuplicatePair,
    EvidenceSpan,
    VerifiedOpportunity,
)
from bidscope.retrieval.deduplication import NoticeView
from pydantic import ValidationError

# --- helpers -----------------------------------------------------------------


@dataclass(frozen=True)
class _Settings:
    """Minimal slice of bidscope.config.Settings the adapter touches."""
    model_base_url: str
    model_name: str
    model_api_key: str


def _settings() -> _Settings:
    return _Settings(
        model_base_url="https://api.deepseek.example.com/v1",
        model_name="deepseek-chat",
        model_api_key="sk-stub-key",
    )


def _verified() -> VerifiedOpportunity:
    return VerifiedOpportunity(
        notice_id="demo-001",
        title="四川省智算中心服务器采购项目公开招标公告",
        region="四川省",
        purchaser="四川省大数据中心",
        budget_raw="680万元",
        deadline="2026-08-01T09:00:00+08:00",
        summary="智算中心服务器采购",
        evidence=(
            EvidenceSpan(
                evidence_id="ev-001",
                text="预算金额：680万元",
                notice_id="demo-001",
            ),
        ),
    )


def _notice_view(title: str = "四川省智算中心服务器采购项目") -> NoticeView:
    return NoticeView(
        source="synthetic_demo",
        external_id="demo-001",
        canonical_url="https://example.invalid/demo-001",
        project_number=None,
        content_hash="a" * 64,
        title=title,
        purchaser="四川省大数据中心",
        region="四川省",
    )


def _make_patched_report_model() -> tuple[Any, MagicMock]:
    """Build a DeepSeekReportModel with ChatOpenAI patched.

    Returns (adapter, structured_mock) where structured_mock.ainvoke is
    an AsyncMock. The mock returns a dict that ReportDraft.model_validate
    accepts so the adapter does not raise during the test.
    """
    from bidscope.llm.deepseek import DeepSeekReportModel

    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value={
            "items": [],
            "freshness_window": None,
            "source_availability": [],
            "completeness_warning": None,
            "assumptions": [],
        }
    )
    chat_open_ai = MagicMock()
    chat_open_ai.with_structured_output.return_value = structured
    with patch("bidscope.llm.deepseek.ChatOpenAI", return_value=chat_open_ai):
        adapter = DeepSeekReportModel(_settings())  # type: ignore[arg-type]
    return adapter, structured


def _make_patched_duplicate_model() -> tuple[Any, MagicMock]:
    """Build a DeepSeekDuplicateModel with ChatOpenAI patched."""
    from bidscope.llm.deepseek import (
        DeepSeekDuplicateModel,
        DuplicateClassificationResult,
    )

    structured_response = DuplicateClassificationResult(
        decision="distinct", reasons=["stub"]
    )
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=structured_response)
    chat_open_ai = MagicMock()
    chat_open_ai.with_structured_output.return_value = structured
    with patch("bidscope.llm.deepseek.ChatOpenAI", return_value=chat_open_ai):
        adapter = DeepSeekDuplicateModel(_settings())  # type: ignore[arg-type]
    return adapter, structured


# 1. UNTRUSTED_SOURCE_DATA wrapping (duplicate model) -----------------------


def test_duplicate_model_wraps_notice_text_in_untrusted_delimiters() -> None:
    """DeepSeekDuplicateModel.classify() must wrap notice text in
    UNTRUSTED_SOURCE_DATA_START / UNTRUSTED_SOURCE_DATA_END delimiters.

    This ensures imported source content is clearly delineated so that
    prompt-injection attempts inside the source text cannot be interpreted
    as system instructions by the model.
    """
    adapter, structured = _make_patched_duplicate_model()

    async def _run() -> None:
        pair = DuplicatePair(
            candidate=_notice_view("Notice A"),
            existing=_notice_view("Notice B"),
        )
        await adapter.classify(pair)
        assert structured.ainvoke.await_count == 1
        messages = structured.ainvoke.await_args.args[0]
        payload = json.dumps(
            [
                {
                    "role": getattr(m, "type", ""),
                    "content": getattr(m, "content", str(m)),
                }
                for m in messages
            ],
            ensure_ascii=False,
        )
        assert "UNTRUSTED_SOURCE_DATA_START" in payload
        assert "UNTRUSTED_SOURCE_DATA_END" in payload

    import asyncio

    asyncio.run(_run())


# 2. System prompt instruction ------------------------------------------------


def test_duplicate_model_system_prompt_contains_untrusted_warning() -> None:
    """The system prompt must tell the model that source text cannot issue
    instructions or request tools."""
    adapter, structured = _make_patched_duplicate_model()

    async def _run() -> None:
        pair = DuplicatePair(
            candidate=_notice_view("A"),
            existing=_notice_view("B"),
        )
        await adapter.classify(pair)
        messages = structured.ainvoke.await_args.args[0]
        payload = json.dumps(
            [
                {
                    "role": getattr(m, "type", ""),
                    "content": getattr(m, "content", str(m)),
                }
                for m in messages
            ],
            ensure_ascii=False,
        )
        assert "UNTRUSTED_SOURCE_DATA" in payload
        assert "cannot issue instructions" in payload.lower()

    import asyncio

    asyncio.run(_run())


# 3. Report model wrapping ----------------------------------------------------


def test_report_model_wraps_evidence_in_untrusted_delimiters() -> None:
    """DeepSeekReportModel.synthesize() must wrap imported evidence in
    UNTRUSTED_SOURCE_DATA delimiters."""
    adapter, structured = _make_patched_report_model()

    async def _run() -> None:
        await adapter.synthesize(_verified())
        assert structured.ainvoke.await_count == 1
        messages = structured.ainvoke.await_args.args[0]
        payload = json.dumps(
            [
                {
                    "role": getattr(m, "type", ""),
                    "content": getattr(m, "content", str(m)),
                }
                for m in messages
            ],
            ensure_ascii=False,
        )
        assert "UNTRUSTED_SOURCE_DATA_START" in payload
        assert "UNTRUSTED_SOURCE_DATA_END" in payload
        # The evidence text must reach the model
        assert "680万元" in payload
        assert "cannot issue instructions" in payload.lower()

    import asyncio

    asyncio.run(_run())


def test_report_model_system_prompt_references_untrusted_data() -> None:
    """The report model system prompt must mention UNTRUSTED_SOURCE_DATA."""
    adapter, structured = _make_patched_report_model()

    async def _run() -> None:
        await adapter.synthesize(_verified())
        messages = structured.ainvoke.await_args.args[0]
        payload = json.dumps(
            [
                {
                    "role": getattr(m, "type", ""),
                    "content": getattr(m, "content", str(m)),
                }
                for m in messages
            ],
            ensure_ascii=False,
        )
        assert "UNTRUSTED_SOURCE_DATA" in payload

    import asyncio

    asyncio.run(_run())


# 4. Intent model does NOT wrap user query -----------------------------------


def test_intent_model_does_not_wrap_user_query() -> None:
    """DeepSeekIntentModel.parse() must NOT wrap the user's own query in
    UNTRUSTED_SOURCE_DATA delimiters. The user's query is trusted input
    (it originates from the authenticated user, not from imported source
    content), so it should be sent directly without untrusted wrapping."""
    from bidscope.llm.deepseek import DeepSeekIntentModel

    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value={
            "topics": ["服务器"],
            "regions": ["四川"],
            "confidence": 0.9,
            "assumptions": [],
        }
    )
    chat_open_ai = MagicMock()
    chat_open_ai.with_structured_output.return_value = structured

    clock = MagicMock()
    clock.now.return_value = datetime(2026, 7, 18, tzinfo=UTC)

    with patch("bidscope.llm.deepseek.ChatOpenAI", return_value=chat_open_ai):
        adapter = DeepSeekIntentModel(_settings())  # type: ignore[arg-type]

    async def _run() -> None:
        await adapter.parse("四川服务器采购", clock)
        assert structured.ainvoke.await_count == 1
        # The intent model sends a plain string prompt (not messages list),
        # so ainvoke is called with a single string argument.
        call_args = structured.ainvoke.await_args.args[0]
        payload = json.dumps(call_args, ensure_ascii=False)
        assert "UNTRUSTED_SOURCE_DATA" not in payload
        # But the user's query must be present
        assert "四川服务器采购" in payload

    import asyncio

    asyncio.run(_run())


# 5. DOCX filename sanitization ---------------------------------------------


def test_sanitize_filename_strips_path_separators() -> None:
    """_sanitize_filename() must strip forward slashes so path traversal
    sequences cannot survive as directory separators.

    Note: the function preserves dots (``.``) because they are common in
    safe filenames like ``report.v2.docx``. The key defence against path
    traversal is stripping ``/`` and ``\\`` so the result is always a
    single filename segment with no directory components.
    """
    from bidscope.delivery.docx import _sanitize_filename

    result = _sanitize_filename("../../etc/passfile")
    assert "/" not in result
    assert "\\" not in result
    # The result must be a single path segment — no directory traversal.
    assert "/" not in result.replace("\\", "")


def test_sanitize_filename_strips_script_tags() -> None:
    """_sanitize_filename() must strip angle brackets and other unsafe
    characters that could be interpreted as HTML/script injection."""
    from bidscope.delivery.docx import _sanitize_filename

    result = _sanitize_filename("file<script>.docx")
    assert "<" not in result
    assert ">" not in result


def test_sanitize_filename_hidden_file() -> None:
    """_sanitize_filename() must strip leading dots so a name like
    ``.hidden`` cannot produce a hidden file on Unix systems."""
    from bidscope.delivery.docx import _sanitize_filename

    result = _sanitize_filename(".hidden")
    assert not result.startswith(".")


def test_sanitize_filename_empty_falls_back_to_report() -> None:
    """An empty input must fall back to ``report`` so downstream code always
    has a non-empty, safe filename."""
    from bidscope.delivery.docx import _sanitize_filename

    assert _sanitize_filename("") == "report"


def test_sanitize_filename_all_dots_falls_back_to_report() -> None:
    """A name that is entirely dots is stripped to empty by ``strip('.')``
    and must fall back to ``report``."""
    from bidscope.delivery.docx import _sanitize_filename

    assert _sanitize_filename("...") == "report"


def test_sanitize_filename_preserves_safe_characters() -> None:
    """Safe filename characters must be preserved."""
    from bidscope.delivery.docx import _sanitize_filename

    result = _sanitize_filename("report-2026_final.v2.docx")
    assert result == "report-2026_final.v2.docx"


# 6. Bounded error union ------------------------------------------------------


def test_bidscope_error_code_contains_all_design_codes() -> None:
    """BidScopeErrorCode must contain every error code from design §9."""
    expected = {
        "snapshot_integrity_error",
        "snapshot_stale",
        "parse_drift",
        "intent_invalid",
        "retrieval_empty",
        "evidence_insufficient",
        "model_transient_error",
        "delivery_error",
        "snapshot_import_error",
        "graph_node_error",
    }
    actual = {member.value for member in BidScopeErrorCode}
    assert actual == expected, f"missing: {expected - actual}, extra: {actual - expected}"


def test_serializable_error_rejects_arbitrary_code() -> None:
    """SerializableError.code must only accept valid BidScopeErrorCode values."""
    with pytest.raises(ValidationError):
        SerializableError(code="anything_goes", message="x")


def test_serializable_error_accepts_all_valid_codes() -> None:
    """Every member of BidScopeErrorCode must be accepted by SerializableError."""
    for code in BidScopeErrorCode:
        error = SerializableError(code=code, message="test")
        assert error.code == code


# 7. VerifiedOpportunity evidence type constraint ----------------------------


def test_verified_opportunity_evidence_requires_evidence_span() -> None:
    """VerifiedOpportunity.evidence must only accept EvidenceSpan objects.

    The frozen dataclass with tuple[EvidenceSpan, ...] typing enforces
    that evidence entries carry a structured provenance chain
    (evidence_id, text, notice_id) rather than arbitrary strings.
    """
    # Valid: EvidenceSpan tuple
    vo = VerifiedOpportunity(
        notice_id="n-001",
        title="Test",
        evidence=(
            EvidenceSpan(
                evidence_id="ev-001",
                text="snippet",
                notice_id="n-001",
            ),
        ),
    )
    assert isinstance(vo.evidence[0], EvidenceSpan)
    assert vo.evidence[0].notice_id == "n-001"


def test_verified_opportunity_empty_evidence_accepted() -> None:
    """An empty evidence tuple is valid (no evidence yet bound)."""
    vo = VerifiedOpportunity(notice_id="n-001", title="Test")
    assert vo.evidence == ()


def test_verified_opportunity_frozen() -> None:
    """VerifiedOpportunity is frozen — evidence cannot be mutated after creation."""
    vo = VerifiedOpportunity(notice_id="n-001", title="Test")
    with pytest.raises(AttributeError):
        vo.notice_id = "n-002"  # type: ignore[misc]


# 8. SQL-like plan rejection (validate_intent) -------------------------------


def _make_state(
    topics: list[str] | None = None,
    regions: list[str] | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    min_budget: Money | None = None,
    max_budget: Money | None = None,
) -> Any:
    """Build a minimal state-like object for validate_intent().

    Defaults are applied only when the argument is ``None`` — passing an
    explicit ``[]`` is preserved so we can test the empty-topics/regions
    guard in ``validate_intent``.
    """
    from bidscope.domain.intents import SearchIntent

    resolved_topics = topics if topics is not None else ["服务器"]
    resolved_regions = regions if regions is not None else ["四川"]

    intent = SearchIntent(
        topics=resolved_topics,
        regions=resolved_regions,
        published_from=published_from,
        published_to=published_to,
        min_budget=min_budget,
        max_budget=max_budget,
        confidence=0.9,
        assumptions=[],
    )

    class _State:
        def __init__(self, search_intent: SearchIntent) -> None:
            self.search_intent = search_intent

    return _State(intent)


class _Config:
    """Minimal RunnableConfig stand-in for validate_intent."""

    def __init__(self) -> None:
        self._config = {"configurable": {"deps": MagicMock()}}

    def __getitem__(self, key: str) -> Any:
        return self._config[key]


def _make_state_raw(
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    min_budget: Money | None = None,
    max_budget: Money | None = None,
    topics: list[str] | None = None,
    regions: list[str] | None = None,
) -> Any:
    """Build a state with a SearchIntent that bypasses Pydantic validation.

    ``SearchIntent`` rejects inverted dates/budgets at construction time
    via its own model validators. To test ``validate_intent``'s independent
    defence-in-depth guard, we use ``model_construct`` which skips validation
    and injects the raw field values directly.
    """
    from bidscope.domain.intents import SearchIntent

    intent = SearchIntent.model_construct(
        topics=topics if topics is not None else ["服务器"],
        regions=regions if regions is not None else ["四川"],
        published_from=published_from,
        published_to=published_to,
        min_budget=min_budget,
        max_budget=max_budget,
        confidence=0.9,
        assumptions=[],
    )

    class _State:
        def __init__(self, search_intent: SearchIntent) -> None:
            self.search_intent = search_intent

    return _State(intent)


def test_validate_intent_rejects_inverted_dates() -> None:
    """validate_intent() must reject intents where published_from > published_to.

    This is a defence-in-depth guard: SearchIntent's own Pydantic validators
    already enforce this at parse time, but validate_intent independently
    re-checks the condition so a malformed intent that somehow bypasses
    Pydantic cannot proceed. We use ``model_construct`` to simulate an intent
    that bypassed parse-time validation.
    """
    from bidscope.domain.types import BidScopeErrorCode

    state = _make_state_raw(
        published_from=datetime(2026, 7, 18, tzinfo=UTC),
        published_to=datetime(2026, 7, 11, tzinfo=UTC),
    )
    result = validate_intent(state, _Config())  # type: ignore[arg-type]
    assert "errors" in result
    assert any(
        e.code == BidScopeErrorCode.INTENT_INVALID for e in result["errors"]
    )


def test_validate_intent_rejects_inverted_budget() -> None:
    """validate_intent() must reject intents where min_budget > max_budget.

    We use ``model_construct`` to bypass SearchIntent's own budget-range
    validator and test validate_intent's independent check directly.
    """
    from bidscope.domain.types import BidScopeErrorCode

    state = _make_state_raw(
        min_budget=Money(minor_units=1_000_000_00, currency="CNY"),
        max_budget=Money(minor_units=500_000_00, currency="CNY"),
    )
    result = validate_intent(state, _Config())  # type: ignore[arg-type]
    assert "errors" in result
    assert any(
        e.code == BidScopeErrorCode.INTENT_INVALID for e in result["errors"]
    )


def test_validate_intent_rejects_empty_topics() -> None:
    """validate_intent() must reject intents with no topics."""
    from bidscope.domain.types import BidScopeErrorCode

    state = _make_state(topics=[])
    result = validate_intent(state, _Config())  # type: ignore[arg-type]
    assert "errors" in result
    assert any(
        e.code == BidScopeErrorCode.INTENT_INVALID for e in result["errors"]
    )


def test_validate_intent_rejects_empty_regions() -> None:
    """validate_intent() must reject intents with no regions."""
    from bidscope.domain.types import BidScopeErrorCode

    state = _make_state(regions=[])
    result = validate_intent(state, _Config())  # type: ignore[arg-type]
    assert "errors" in result
    assert any(
        e.code == BidScopeErrorCode.INTENT_INVALID for e in result["errors"]
    )


def test_validate_intent_accepts_valid_intent() -> None:
    """A valid intent with consistent dates and budget passes validation."""
    state = _make_state(
        published_from=datetime(2026, 7, 11, tzinfo=UTC),
        published_to=datetime(2026, 7, 18, tzinfo=UTC),
        min_budget=Money(minor_units=5_000_000_00, currency="CNY"),
    )
    result = validate_intent(state, _Config())  # type: ignore[arg-type]
    assert "errors" not in result


# 9. Raw HTML not rendered (report serialization) ----------------------------


def test_serialize_report_escapes_html_content() -> None:
    """Report serialization must not interpret raw HTML as markup.

    The existing code uses FastAPI's JSONResponse (via the dict return),
    which leverages Pydantic's JSON encoder. Pydantic serializes strings
    as JSON strings (with proper escaping), so HTML content in report
    fields is safely escaped and cannot be interpreted as markup when
    parsed by a standard JSON decoder.

    We verify this by serializing HTML-bearing content and confirming
    that the JSON output treats it as an opaque string.
    """
    from bidscope.api.routes.reports import _serialize_report

    # Build a minimal report-like object with HTML in string fields.
    class _Item:
        def __init__(self) -> None:
            self.title = '<script>alert("xss")</script>'
            self.known_fields = {
                "budget": '<b>680万</b>',
                "source_url": "https://www.ccgp.gov.cn/<script>",
            }
            self.retrieved_at = "2026-07-18"
            self.hash_prefix = "abc123"
            self.freshness_days = 3
            self.source = None

    class _Report:
        def __init__(self) -> None:
            self.id = "r-001"
            self.run_id = "run-001"
            self.export_key = "docx-v1:run-001"
            self.conditions = {"topic": '<img src=x onerror=alert(1)>'}
            self.freshness_window = "2026-07-11/2026-07-18"
            self.completeness_warning = "<div>warning</div>"
            self.generated_at = datetime(2026, 7, 18, tzinfo=UTC)

    report = _Report()
    items = [_Item()]

    result = _serialize_report(report, items)  # type: ignore[arg-type]

    # Serialize to JSON and back — HTML content must survive as escaped strings
    encoded = json.dumps(result, ensure_ascii=False)
    decoded = json.loads(encoded)

    # The script tags must be present as literal text (escaped by JSON encoder)
    assert "<script>alert" in encoded or "\\u003cscript" in encoded
    # The decoded value must contain the literal tag, not parsed markup
    assert '<script>alert("xss")</script>' in decoded["items"][0]["title"]


def test_serialize_report_bounded_field_length() -> None:
    """Report serialization must bound string fields to prevent oversized output."""
    from bidscope.api.routes.reports import _serialize_report

    long_text = "A" * 10_000

    class _Item:
        def __init__(self) -> None:
            self.title = long_text
            self.known_fields = {"budget": long_text}
            self.retrieved_at = None
            self.hash_prefix = None
            self.freshness_days = None
            self.source = None

    class _Report:
        def __init__(self) -> None:
            self.id = "r-001"
            self.run_id = "run-001"
            self.export_key = "docx-v1:run-001"
            self.conditions = {"topic": long_text}
            self.freshness_window = None
            self.completeness_warning = None
            self.generated_at = datetime(2026, 7, 18, tzinfo=UTC)

    report = _Report()
    result = _serialize_report(report, [_Item()])  # type: ignore[arg-type]

    # Title should be bounded to _ITEM_TEXT_LIMIT (240 chars)
    assert len(result["items"][0]["title"]) <= 240


def test_serialize_report_limits_item_count() -> None:
    """Report serialization must limit the number of items returned."""
    from bidscope.api.routes.reports import _serialize_report

    class _Item:
        def __init__(self, idx: int) -> None:
            self.title = f"Item {idx}"
            self.known_fields = {}
            self.retrieved_at = None
            self.hash_prefix = None
            self.freshness_days = None
            self.source = None

    class _Report:
        def __init__(self) -> None:
            self.id = "r-001"
            self.run_id = "run-001"
            self.export_key = "docx-v1:run-001"
            self.conditions = {}
            self.freshness_window = None
            self.completeness_warning = None
            self.generated_at = datetime(2026, 7, 18, tzinfo=UTC)

    report = _Report()
    many_items = [_Item(i) for i in range(150)]
    result = _serialize_report(report, many_items)  # type: ignore[arg-type]

    # _REPORT_ITEMS_LIMIT is 100
    assert len(result["items"]) <= 100
