"""Shared, pure parsing helpers for snapshot adapters.

The adapters for each source are intentionally source-specific in their
HTML/JSON extraction rules, but they share the deterministic text-level
normalisation in this module: whitespace cleaning, Chinese-currency money
parsing and datetime parsing. Keeping these in one place guarantees that
"680万元" always means the same integer minor units no matter which adapter
produced the record.

The :class:`ParseDrift` exception is the typed, structured diagnostic an
adapter raises when a fixture's structure no longer matches its parser — for
example when a required title element disappears from an official page. Tests
catch this instead of bare ``KeyError``\\ s or ``AttributeError``\\ s.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.notices import Money, NormalizedNotice
from bidscope.domain.snapshots import SnapshotManifest
from pydantic import HttpUrl

CHN_TZ = timezone(timedelta(hours=8))
CNY = "CNY"

#: Label patterns found in source pages/records, mapped to the
#: :class:`NormalizedNotice` field they populate. Each rule lists every
#: substring that may label that field ("发布时间" and "公告时间" both mean
#: publish time, for example); an adapter matches a row's label against the
#: first rule whose substring it contains, so the rule lives in exactly one
#: place.
FIELD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("项目编号",), "external_id"),
    (("采购人",), "purchaser"),
    (("地区",), "region"),
    (("预算",), "budget"),
    (("发布", "公告"), "publish_time"),
    (("截止",), "deadline"),
)


class ParseDrift(Exception):
    """Raised when a fixture's structure does not match the parser's expectations.

    Carries a stable ``path`` (the field or element that drifted) and optional
    ``detail`` so callers can assert on the diagnostic rather than parsing
    exception messages.
    """

    def __init__(
        self,
        message: str,
        path: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.detail = detail


def normalize_whitespace(text: str | None) -> str | None:
    """Collapse internal whitespace and trim, returning None for blanks."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned or None


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_CHINESE_DT_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def parse_datetime(raw: str | None) -> datetime | None:
    """Parse a source datetime string into a timezone-aware datetime.

    ISO-8601 strings keep their own offset (and stay naive if the input is
    naive — the domain validator then rejects them). Chinese "年月日" forms are
    interpreted in the source's China-time offset, since that is the only
    defensible assumption for an official Chinese tender page.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if _ISO_RE.match(raw):
        return datetime.fromisoformat(raw)

    match = _CHINESE_DT_RE.match(raw)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4)) if match.group(4) else 0
        minute = int(match.group(5)) if match.group(5) else 0
        second = int(match.group(6)) if match.group(6) else 0
        return datetime(year, month, day, hour, minute, second, tzinfo=CHN_TZ)

    return None


_CURRENCY_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿)?\s*元?")


def parse_money(raw: str | None) -> Money | None:
    """Parse a Chinese currency string into integer minor units (分).

    Returns ``None`` rather than guessing when no numeric amount is found, so
    callers preserve the original text in ``raw_fields`` instead of inventing
    a number.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    match = _CURRENCY_RE.search(text)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2)
    if unit == "万":
        yuan = value * 10_000
    elif unit == "亿":
        yuan = value * 100_000_000
    else:
        yuan = value

    minor_units = int(round(yuan * 100))
    return Money(minor_units=minor_units, currency=CNY, raw_text=text)


def _map_field(label: str) -> str | None:
    """Map a source-page label to a NormalizedNotice field name."""
    for substrings, field in FIELD_RULES:
        if any(substring in label for substring in substrings):
            return field
    return None


def load_manifest(bundle: Path) -> SnapshotManifest:
    """Parse and validate a bundle's manifest.json."""
    data = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    return SnapshotManifest.model_validate(data)


def load_expected(bundle: Path) -> list[dict[str, Any]]:
    """Return the human-reviewed expected records from expected.json."""
    data = json.loads((bundle / "expected.json").read_text(encoding="utf-8"))
    return list(data["records"])


def build_notice(
    *,
    source: SourceName,
    capture_kind: CaptureKind,
    parser_version: str,
    source_url: HttpUrl,
    fields: dict[str, Any],
    extra_raw_fields: dict[str, Any] | None = None,
) -> NormalizedNotice:
    """Construct a NormalizedNotice from a normalised fields dict.

    ``fields`` carries the raw, source-shaped strings; this function owns all
    the parsing (datetime, money, whitespace) so each adapter's ``parse``
    stays a thin extraction step.
    """
    raw_budget = fields.get("budget")
    budget = parse_money(raw_budget)
    raw_fields: dict[str, Any] = {}
    if budget is None and raw_budget:
        # Amount text existed but could not be parsed — preserve it rather
        # than silently dropping the only evidence we have.
        raw_fields["budget_text"] = raw_budget
    if extra_raw_fields:
        raw_fields.update(extra_raw_fields)

    publish_time = parse_datetime(fields.get("publish_time"))
    deadline = parse_datetime(fields.get("deadline"))

    return NormalizedNotice(
        source=source,
        external_id=fields.get("external_id") or "",
        source_url=source_url,
        capture_kind=capture_kind,
        title=normalize_whitespace(fields.get("title")),
        purchaser=normalize_whitespace(fields.get("purchaser")),
        region=normalize_whitespace(fields.get("region")),
        publish_time=publish_time,
        deadline=deadline,
        budget=budget,
        cancellation=_parse_cancellation(fields.get("cancellation")),
        parser_version=parser_version,
        raw_fields=raw_fields,
    )


def _parse_cancellation(value: Any) -> bool:
    """Normalize the small, explicit set of source withdrawal markers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {
            "withdrawn",
            "withdrawal",
            "cancelled",
            "canceled",
            "撤回",
            "取消",
            "true",
            "1",
        }
    return False
