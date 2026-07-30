"""Deterministic offline scenario for the authorized ingestion boundary."""

from __future__ import annotations

import pytest
from bidscope.ingestion.ccgp import SourceTimeoutError
from fixtures.authorized_ingestion import ScenarioAuthorizedSourceClient


@pytest.mark.asyncio
async def test_authorized_fixture_covers_pagination_correction_withdrawal_and_recovery() -> None:
    client = ScenarioAuthorizedSourceClient()

    first = await client.fetch_page(None)
    correction_and_withdrawal = await client.fetch_page(first.next_cursor)

    with pytest.raises(SourceTimeoutError):
        await client.fetch_page(correction_and_withdrawal.next_cursor)

    recovered = await client.fetch_page(correction_and_withdrawal.next_cursor)

    assert first.next_cursor == "fixture-cursor-1"
    assert correction_and_withdrawal.next_cursor == "fixture-cursor-2"
    assert correction_and_withdrawal.items == (
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
    assert recovered.next_cursor is None
    assert recovered.items[0]["notice_id"] == "fixture-003"
    assert client.events == [
        "page:none",
        "page:fixture-cursor-1",
        "timeout:fixture-cursor-2",
        "page:fixture-cursor-2",
    ]
