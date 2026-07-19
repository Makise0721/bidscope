"""Contract tests for the deterministic, offline LLM provider.

The fake model never touches a network or an API key. It parses the
representative Chinese query through explicit regex and fixture rules and
returns the same :class:`~bidscope.domain.intents.SearchIntent` on every call.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bidscope.clock import FixedClock
from bidscope.domain.intents import RunSchedule
from bidscope.llm.fake import FakeIntentModel

REPRESENTATIVE_QUERY = (
    "每周一上午 9 点，汇总近 7 天四川和重庆与「智算中心、服务器」有关、"
    "预算 500 万以上的招标信息。"
)


def _fixed_clock() -> FixedClock:
    """A timezone-aware clock pinned to 2026-07-18T09:00 UTC.

    The fake model parses relative to the injected clock, so pinning it makes
    the seven-day window deterministic.
    """
    return FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC))


async def test_parses_representative_query_topics() -> None:
    """The fake model extracts the representative topics from brackets."""
    intent = await FakeIntentModel().parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert "智算中心" in intent.topics
    assert "服务器" in intent.topics


async def test_parses_representative_query_regions() -> None:
    """四川 and 重庆 are recognised as regions."""
    intent = await FakeIntentModel().parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert "四川" in intent.regions
    assert "重庆" in intent.regions


async def test_parses_seven_day_window() -> None:
    """「近 7 天」collapses to a seven-day ``published_from/to`` window."""
    intent = await FakeIntentModel().parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert intent.published_from is not None
    assert intent.published_to is not None
    assert (intent.published_to - intent.published_from).days == 7
    assert intent.published_to.date() == _fixed_clock().now().date()


async def test_parses_five_million_minimum_budget() -> None:
    """「预算 500 万以上」becomes a 5,000,000 CNY floor."""
    intent = await FakeIntentModel().parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert intent.min_budget is not None
    assert intent.min_budget.minor_units == 5_000_000_00
    assert intent.min_budget.currency == "CNY"


async def test_parses_weekly_monday_schedule() -> None:
    """「每周一上午 9 点」becomes a Monday 09:00 cron schedule."""
    intent = await FakeIntentModel().parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert intent.schedule == RunSchedule(
        cron_expression="0 9 * * 1",
        timezone="Asia/Shanghai",
    )


async def test_rejects_empty_request() -> None:
    """Empty input raises rather than returning a silent garbage intent."""
    with pytest.raises(ValueError):
        await FakeIntentModel().parse("   ", _fixed_clock())


async def test_is_deterministic() -> None:
    """The same request always produces the same intent (no RNG, no clock drift)."""
    model = FakeIntentModel()
    first = await model.parse(REPRESENTATIVE_QUERY, _fixed_clock())
    second = await model.parse(REPRESENTATIVE_QUERY, _fixed_clock())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


async def test_returns_model_usage() -> None:
    """Usage metadata records the fake model identity and a positive latency."""
    model = FakeIntentModel()
    await model.parse(REPRESENTATIVE_QUERY, _fixed_clock())
    usage = model.last_usage
    assert usage is not None
    assert usage.model == "fake-deterministic"
    assert usage.latency_ms > 0


async def test_generic_budget_pattern_without_schedule() -> None:
    """A simple no-schedule request still parses the budget floor."""
    request = "查找 300 万元以上的医疗设备采购招标信息"
    intent = await FakeIntentModel().parse(request, _fixed_clock())
    assert intent.min_budget is not None
    assert intent.min_budget.minor_units == 3_000_000_00
    assert intent.min_budget.currency == "CNY"
    assert intent.schedule is None
