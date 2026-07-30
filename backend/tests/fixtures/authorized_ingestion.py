"""Offline authorized-source scenario; it never opens a network connection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

from bidscope.ingestion.ccgp import SourceTimeoutError
from bidscope.ingestion.ports import AuthorizedSourcePage

_RETRIEVED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
_SOURCE_URL = "https://www.ccgp.gov.cn/authorized/v1/fixture-notices"


class ScenarioAuthorizedSourceClient:
    """A scripted source client for pagination, corrections, withdrawals and recovery."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._timeout_remaining = 1

    async def fetch_page(self, cursor: str | None) -> AuthorizedSourcePage:
        label = cursor or "none"
        if cursor == "fixture-cursor-2" and self._timeout_remaining:
            self._timeout_remaining -= 1
            self.events.append(f"timeout:{label}")
            raise SourceTimeoutError()

        self.events.append(f"page:{label}")
        if cursor is None:
            items: tuple[dict[str, object], ...] = (
                {
                    "notice_id": "fixture-001",
                    "title": "Original fixture notice",
                    "deadline": "2026-08-30",
                },
            )
            next_cursor = "fixture-cursor-1"
        elif cursor == "fixture-cursor-1":
            items = (
                {
                    "notice_id": "fixture-001",
                    "title": "Corrected fixture notice",
                    "deadline": "2026-08-31",
                },
                {
                    "notice_id": "fixture-002",
                    "title": "Withdrawn fixture notice",
                    "withdrawn": True,
                },
            )
            next_cursor = "fixture-cursor-2"
        elif cursor == "fixture-cursor-2":
            items = ({"notice_id": "fixture-003", "title": "Recovered fixture notice"},)
            next_cursor = None
        else:
            raise AssertionError(f"unexpected fixture cursor: {cursor}")

        response = json.dumps(
            {"items": items, "next_cursor": next_cursor},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return AuthorizedSourcePage(
            cursor_before=cursor,
            next_cursor=next_cursor,
            items=items,
            response_bytes=response,
            response_sha256=sha256(response).hexdigest(),
            retrieved_at=_RETRIEVED_AT,
            status_code=200,
            source_url=_SOURCE_URL,
        )
