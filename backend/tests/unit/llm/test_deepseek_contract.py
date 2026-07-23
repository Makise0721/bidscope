"""Contract tests for the OpenAI-compatible (DeepSeek) provider.

These tests prove three things without any network access and without a real
API key:

1. The adapter only needs a stub key to build — it never reaches the network
   during test collection or invocation.
2. It routes through ``with_structured_output`` against a Pydantic schema.
3. It wraps every imported source span in an ``UNTRUSTED_SOURCE_DATA`` section
   and returns a typed :class:`ReportDraft` plus :class:`ModelUsage`.

ChatOpenAI is patched at the module boundary so even the ``AsyncOpenAI``
constructor inside langchain-openai never runs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bidscope.llm.deepseek import DeepSeekDuplicateModel, DeepSeekReportModel
from bidscope.llm.types import (
    DuplicatePair,
    EvidenceSpan,
    ModelUsage,
    ReportDraft,
    VerifiedOpportunity,
)
from bidscope.retrieval.deduplication import DuplicateClassification, NoticeView


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
            EvidenceSpan(evidence_id="ev-001", text="预算金额：680万元", notice_id="demo-001"),
        ),
    )


def _draft_json() -> dict:
    return {
        "items": [
            {
                "notice_id": "demo-001",
                "title": "四川省智算中心服务器采购项目公开招标公告",
                "known_fields": {"region": "四川省", "purchaser": "四川省大数据中心"},
                "unknown_fields": [],
                "relevance_reason": "Matches 服务器 topic and 四川 region",
                "risk_note": None,
                "citations": [{"evidence_id": "ev-001", "label": "budget"}],
                "claims": [
                    {
                        "text": "预算 680 万元",
                        "citation_ids": ["ev-001"],
                    }
                ],
            }
        ],
        "freshness_window": "2026-07-11/2026-07-18",
        "source_availability": ["synthetic_demo"],
        "completeness_warning": None,
        "assumptions": [],
    }


def _make_patched_model(settings: _Settings) -> tuple[DeepSeekReportModel, MagicMock]:
    """Return (adapter, structured_llm_mock) with ChatOpenAI patched.

    The ``structured_llm_mock`` is the object returned by
    ``with_structured_output``; its ``ainvoke`` is an ``AsyncMock``.
    """
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=ReportDraft.model_validate(_draft_json()))
    chat_open_ai = MagicMock()
    chat_open_ai.with_structured_output.return_value = structured
    with patch("bidscope.llm.deepseek.ChatOpenAI", return_value=chat_open_ai) as patched:
        adapter = DeepSeekReportModel(settings)  # type: ignore[arg-type]
    _ = patched
    return adapter, structured


async def test_builds_without_real_key_or_network() -> None:
    """Construction must not raise and must not touch the network."""
    adapter, _ = _make_patched_model(_settings())
    assert adapter is not None


async def test_routes_through_structured_output() -> None:
    """The adapter must call ``with_structured_output`` with the draft schema."""
    settings = _settings()
    adapter, structured = _make_patched_model(settings)
    draft = await adapter.synthesize(_verified())
    assert isinstance(draft, ReportDraft)
    assert len(draft.items) == 1
    assert draft.items[0].claims[0].citation_ids == ["ev-001"]


def test_report_draft_rejects_duplicate_claim_citation_ids() -> None:
    payload = _draft_json()
    payload["items"][0]["claims"][0]["citation_ids"] = ["ev-001", "ev-001"]

    with pytest.raises(ValueError, match="duplicate citation_ids"):
        ReportDraft.model_validate(payload)


async def test_wraps_source_text_in_untrusted_section() -> None:
    """Imported evidence spans must be wrapped in UNTRUSTED_SOURCE_DATA."""
    settings = _settings()
    adapter, structured = _make_patched_model(settings)
    await adapter.synthesize(_verified())
    assert structured.ainvoke.await_count == 1
    messages = structured.ainvoke.await_args.args[0]
    envelope = [
        {"role": getattr(m, "type", ""), "content": getattr(m, "content", str(m))}
        for m in messages
    ]
    payload = json.dumps(envelope, ensure_ascii=False)
    assert "UNTRUSTED_SOURCE_DATA" in payload
    assert "680万元" in payload  # the evidence text must reach the model
    assert "cannot issue instructions" in payload.lower()


async def test_returns_model_usage_with_latency() -> None:
    """The adapter records a typed ModelUsage capturing model and latency."""
    adapter, _ = _make_patched_model(_settings())
    await adapter.synthesize(_verified())
    usage = adapter.last_usage
    assert isinstance(usage, ModelUsage)
    assert usage.model == "deepseek-chat"
    assert usage.latency_ms > 0
    assert usage.prompt_tokens >= 0
    assert usage.completion_tokens >= 0


def test_import_time_does_not_require_real_key() -> None:
    """Importing the module with no key configured must not raise."""
    from bidscope.llm import deepseek as deepseek_module  # noqa: F401

    assert deepseek_module.DeepSeekReportModel is not None


def test_synthesis_is_async() -> None:
    """``synthesize`` is awaitable, not a synchronous call."""
    coro = DeepSeekReportModel(_settings()).synthesize(_verified())
    assert asyncio.iscoroutine(coro)
    coro.close()  # avoid "never awaited" warning


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


async def test_deepseek_duplicate_classify_calls_api() -> None:
    """DeepSeekDuplicateModel.classify() must actually invoke the model port."""
    from bidscope.llm.deepseek import DuplicateClassificationResult

    structured_response = DuplicateClassificationResult(decision="ambiguous", reasons=["stub"])
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=structured_response)
    chat_open_ai = MagicMock()
    chat_open_ai.with_structured_output.return_value = structured
    with patch("bidscope.llm.deepseek.ChatOpenAI", return_value=chat_open_ai):
        adapter = DeepSeekDuplicateModel(_settings())
    pair = DuplicatePair(candidate=_notice_view("A"), existing=_notice_view("B"))
    result = await adapter.classify(pair)
    assert isinstance(result, DuplicateClassification)
    assert result.decision == "ambiguous"
    assert result.reasons == ("stub",)
    assert structured.ainvoke.await_count == 1, "the model port must be called exactly once"
    usage = adapter.last_usage
    assert isinstance(usage, ModelUsage)
    assert usage.model == "deepseek-chat"
    assert usage.latency_ms > 0


__all__ = ["DuplicateClassification", "ModelUsage", "ReportDraft", "VerifiedOpportunity"]
