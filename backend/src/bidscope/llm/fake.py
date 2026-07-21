"""Deterministic, fully offline LLM provider.

Three concrete implementations of the :mod:`bidscope.llm.ports` protocols.
They share one design rule: every output is a pure function of its inputs plus
this file's regex tables. There is no randomness, no system clock, no network
call and no API key. That makes the public demo reproducible and keeps the
graph test suite fast and offline.

The intent parser is deliberately conservative: when the request does not name
a region, a budget or a schedule, those fields stay ``None`` rather than being
invented. The representative Chinese query exercises every branch at once.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from bidscope.clock import Clock
from bidscope.domain.intents import RunSchedule, SearchIntent
from bidscope.domain.notices import Money
from bidscope.domain.reports import ReportCitation, ReportClaim, ReportItem
from bidscope.llm.types import (
    DuplicatePair,
    ModelUsage,
    ReportDraft,
    VerifiedOpportunity,
)
from bidscope.retrieval.deduplication import DuplicateClassification

#: `` fen`` per unit. Money is stored in integer minor units (fen): 1 yuan = 100.
_FEN_PER_YUAN = 100
_FEN_PER_WAN = 10_000 * _FEN_PER_YUAN
_FEN_PER_YI = 100_000_000 * _FEN_PER_YUAN

_REGION_ORDER = (
    "黑龙江", "内蒙古", "宁夏", "新疆", "青海", "甘肃", "西藏", "云南", "贵州", "辽宁",
    "吉林", "河北", "山西", "陕西", "广西", "海南", "河南", "湖北", "湖南", "广东",
    "福建", "江西", "山东", "安徽", "江苏", "浙江",
    "四川", "重庆", "北京", "上海", "天津",
)
_REGIONS = (
    "四川", "重庆", "北京", "上海", "天津", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
    "海南", "贵州", "云南", "陕西", "甘肃", "青海", "内蒙古", "广西", "西藏", "宁夏",
    "新疆",
)

#: Topic keywords grouped by synonyms. ``match_terms`` are the substrings that
 #: trigger the group; ``expanded`` is everything the search should widen to.
_TOPIC_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("智算中心", "智算", "算力中心"), ("智算中心", "算力中心", "数据中心", "IDC")),
    (("服务器", "服務器"), ("服务器", "服务器集群", "GPU 服务器")),
    (("存储", "存储系统"), ("存储", "分布式存储", "SAN")),
    (("网络", "交换机", "路由器"), ("网络", "交换机", "路由器", "核心网")),
    (("医疗", "医疗设备"), ("医疗", "医疗设备", "医疗器械")),
    (("教育", "校园"), ("教育", "校园", "高校")),
)

_BUDGET_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿|元)")
_SCHEDULE_WEEKLY = re.compile(r"每周")
_SCHEDULE_MONTHLY = re.compile(r"每月")
_DAY_OF_WEEK = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
}
_TIME_PATTERN = re.compile(r"([01]?\d|2[0-3])\s*[点時:：]\s*([0-5]?\d)?")
_RECENT_DAYS = re.compile(r"近\s*(\d+)\s*天")
_LOOKBACK_KEYWORDS = ("近", "最近", "过去")
_QUOTED_PATTERN = re.compile(r"[「『]([^「『』」]+)[』」]")


class FakeIntentModel:
    """Deterministic intent parser — no network, no API key."""

    def __init__(self) -> None:
        self._region_re = re.compile("|".join(_REGIONS))
        self._last_usage: ModelUsage | None = None

    @property
    def last_usage(self) -> ModelUsage | None:
        """Return the receipt from the most recent invocation, if any."""
        return self._last_usage

    async def parse(self, request: str, clock: Clock) -> SearchIntent:
        """Extract topics, regions, budget, dates and schedule from ``request``."""
        if not request or not request.strip():
            raise ValueError("request must not be empty")

        now = clock.now()
        topics, expanded = self._extract_topics(request)
        regions = self._extract_regions(request)
        min_budget = self._extract_min_budget(request)
        max_budget = self._extract_max_budget(request)
        published_from, published_to = self._extract_window(request, now)
        schedule = self._extract_schedule(request)

        self._last_usage = ModelUsage(
            model="fake-deterministic",
            prompt_tokens=len(request),
            completion_tokens=len(topics) + len(regions),
            latency_ms=1.0,
            pricing_snapshot="offline",
        )

        return SearchIntent(
            topics=topics,
            expanded_terms=expanded,
            regions=regions,
            published_from=published_from,
            published_to=published_to,
            min_budget=min_budget,
            max_budget=max_budget,
            schedule=schedule,
            confidence=1.0,
            assumptions=[],
        )

    def _extract_topics(self, request: str) -> tuple[list[str], list[str]]:
        quoted = _QUOTED_PATTERN.findall(request)
        if quoted:
            tokens: list[str] = []
            for segment in quoted:
                tokens.extend(part for part in re.split(r"[、,，]", segment) if part.strip())
            topics = [t.strip() for t in tokens if t.strip()]
            expanded = list(topics)
            return topics, expanded

        keyword_topics: list[str] = []
        keyword_expanded: list[str] = []
        for match_terms, group_expanded in _TOPIC_GROUPS:
            if any(term in request for term in match_terms):
                keyword_topics.extend(match_terms)
                keyword_expanded.extend(group_expanded)
        if keyword_topics:
            return list(dict.fromkeys(keyword_topics)), list(dict.fromkeys(keyword_expanded))
        return ["招标"], ["招标"]

    def _extract_regions(self, request: str) -> list[str]:
        candidates = [m for m in _REGION_ORDER if m in request]
        seen: set[str] = set()
        ordered: list[str] = []
        for region in candidates:
            if any(region in kept and region != kept for kept in candidates):
                continue
            if region not in seen:
                seen.add(region)
                ordered.append(region)
        return ordered

    def _parse_budget_to_fen(self, amount_text: str, unit: str) -> int:
        amount = float(amount_text)
        if unit == "亿":
            return int(amount * _FEN_PER_YI)
        if unit == "万":
            return int(amount * _FEN_PER_WAN)
        return int(amount * _FEN_PER_YUAN)

    def _extract_min_budget(self, request: str) -> Money | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿|元)\s*元?\s*以?上", request)
        if not match:
            return None
        return Money(
            minor_units=self._parse_budget_to_fen(match.group(1), match.group(2)),
            currency="CNY",
            raw_text=match.group(0),
        )

    def _extract_max_budget(self, request: str) -> Money | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(万|亿|元)\s*元?\s*以?下", request)
        if not match:
            return None
        return Money(
            minor_units=self._parse_budget_to_fen(match.group(1), match.group(2)),
            currency="CNY",
            raw_text=match.group(0),
        )

    def _extract_window(
        self, request: str, now: datetime
    ) -> tuple[Any, Any]:
        """Return ``(published_from, published_to)``.

        The representative query uses the ``近 N 天`` form. A bare number of
        days without an explicit lookback keyword is intentionally ignored so
        that unrelated digit groups do not create a phantom window. When no
        window is requested, both elements stay ``None``.
        """
        if not any(keyword in request for keyword in _LOOKBACK_KEYWORDS):
            return None, None
        days_match = _RECENT_DAYS.search(request)
        if not days_match:
            return None, None

        days = int(days_match.group(1))
        published_to = now.replace(hour=0, minute=0, second=0, microsecond=0)
        published_from = published_to - timedelta(days=days)
        return published_from, published_to

    def _extract_schedule(self, request: str) -> RunSchedule | None:
        has_weekly = _SCHEDULE_WEEKLY.search(request)
        has_monthly = _SCHEDULE_MONTHLY.search(request)
        if not has_weekly and not has_monthly:
            return None
        time_match = _TIME_PATTERN.search(request)
        day_match = re.search(r"周([一二三四五六日天])", request)
        hour = int(time_match.group(1)) if time_match else 9
        minute = int(time_match.group(2)) if time_match and time_match.group(2) else 0
        day_of_week = _DAY_OF_WEEK[day_match.group(1)] if day_match else 1
        if has_monthly:
            return RunSchedule(
                cron_expression=f"{minute} {hour} 1 * *",
                timezone="Asia/Shanghai",
            )
        return RunSchedule(
            cron_expression=f"{minute} {hour} * * {day_of_week}",
            timezone="Asia/Shanghai",
        )


class FakeDuplicateModel:
    """Stand-in duplicate classifier used by tests.

    The real deterministic classifier
    (:func:`bidscope.retrieval.deduplication.classify_duplicate`) runs before
    the model is consulted, so this fake simply returns a stable ``ambiguous``
    decision. It exists so the graph can instantiate a
    :class:`~bidscope.llm.ports.DuplicateModel` without enabling DeepSeek.
    """

    def __init__(self) -> None:
        self._last_usage: ModelUsage | None = None

    async def classify(self, pair: DuplicatePair) -> DuplicateClassification:
        self._last_usage = ModelUsage(
            model="fake-deterministic",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=1.0,
            pricing_snapshot="offline",
        )
        return DuplicateClassification(decision="ambiguous", reasons=("fake default",))

    @property
    def last_usage(self) -> ModelUsage | None:
        return self._last_usage


class FakeReportModel:
    """Stand-in report synthesizer used by tests.

    Builds one :class:`~bidscope.domain.reports.ReportItem` per verified
    opportunity, quoting only the evidence spans it was given. The DeepSeek
    adapter produces the same shape from a real model call.
    """

    def __init__(self) -> None:
        self._last_usage: ModelUsage | None = None

    async def synthesize(self, verified: VerifiedOpportunity) -> ReportDraft:
        known_fields: dict[str, str] = {}
        unknown_fields: list[str] = []
        if verified.region:
            known_fields["region"] = verified.region
        else:
            unknown_fields.append("region")
        if verified.purchaser:
            known_fields["purchaser"] = verified.purchaser
        else:
            unknown_fields.append("purchaser")
        if verified.budget_raw:
            known_fields["budget"] = verified.budget_raw
        else:
            unknown_fields.append("budget")
        if verified.deadline:
            known_fields["deadline"] = verified.deadline
        else:
            unknown_fields.append("deadline")

        citations = [
            ReportCitation(evidence_id=span.evidence_id, label=span.evidence_id)
            for span in verified.evidence
        ]
        claims = [
            ReportClaim(text=span.text, citation_ids=[span.evidence_id])
            for span in verified.evidence
        ]

        item = ReportItem(
            notice_id=verified.notice_id,
            title=verified.title,
            known_fields=known_fields,
            unknown_fields=unknown_fields,
            relevance_reason=verified.summary,
            risk_note=None,
            citations=citations,
            claims=claims,
        )
        self._last_usage = ModelUsage(
            model="fake-deterministic",
            prompt_tokens=len(verified.evidence),
            completion_tokens=len(claims),
            latency_ms=1.0,
            pricing_snapshot="offline",
        )
        return ReportDraft(
            items=[item],
            freshness_window=None,
            source_availability=[],
            completeness_warning=None,
            assumptions=[],
        )

    @property
    def last_usage(self) -> ModelUsage | None:
        return self._last_usage
