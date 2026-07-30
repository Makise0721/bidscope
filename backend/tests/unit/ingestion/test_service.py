"""Tests for one safe authorized acquisition transaction."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bidscope.clock import FixedClock
from bidscope.domain.snapshots import AuthorizedSourceContract
from bidscope.ingestion.ccgp import SourceRateLimitedError, SourceTimeoutError
from bidscope.ingestion.materializer import BundleQuarantineError
from bidscope.ingestion.ports import AuthorizedSourcePage
from bidscope.ingestion.service import IngestionService

FIXED_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
RESPONSE = b'{"items":[{"notice_id":"n-1"}],"next_cursor":"cursor-2"}'


def _contract() -> AuthorizedSourceContract:
    return AuthorizedSourceContract.model_validate(
        {
            "contract_version": "ccgp-authorized-v1",
            "authorization_ref": "pilot-ccgp-20260730",
            "data_owner": "authorized-operator",
            "regions": ["national"],
            "categories": ["government-procurement"],
            "review_status": "approved",
            "reviewed_at": "2026-07-30T00:00:00+00:00",
            "update_sla": "weekly",
            "retention_days": 365,
        }
    )


def _page(cursor_before: str | None, next_cursor: str | None, suffix: str) -> AuthorizedSourcePage:
    response = f'{{"items":[{{"notice_id":"{suffix}"}}],"next_cursor":null}}'.encode()
    return AuthorizedSourcePage(
        cursor_before=cursor_before,
        next_cursor=next_cursor,
        items=({"notice_id": suffix},),
        response_bytes=response,
        response_sha256=sha256(response).hexdigest(),
        retrieved_at=FIXED_NOW,
        status_code=200,
        source_url="https://www.ccgp.gov.cn/authorized/v1/notices",
    )


class FakeSourceClient:
    def __init__(
        self, pages: list[AuthorizedSourcePage], events: list[str], fail_at: str | None
    ) -> None:
        self.pages = iter(pages)
        self.events = events
        self.fail_at = fail_at

    async def fetch_page(self, cursor: str | None) -> AuthorizedSourcePage:
        self.events.append(f"acquire:{cursor}")
        if self.fail_at == "acquire":
            raise SourceTimeoutError()
        return next(self.pages)


class RateLimitedSourceClient:
    async def fetch_page(self, _cursor: str | None) -> AuthorizedSourcePage:
        raise SourceRateLimitedError(30)


class FakeRepository:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at
        self.cursor = SimpleNamespace(
            source="ccgp", cursor_value="cursor-1", version=4, watermark_at=FIXED_NOW
        )
        self.run = SimpleNamespace(id="run-1")
        self.advanced = False
        self.finalized_status: str | None = None

    async def get_or_create_source_sync_cursor(self, **kwargs: Any) -> Any:
        self.events.append("cursor_read")
        return self.cursor

    async def create_acquisition_run(self, **kwargs: Any) -> Any:
        self.events.append("run_created")
        return self.run

    async def advance_source_sync_cursor(self, **kwargs: Any) -> bool:
        self.events.append("cursor_advance")
        if self.fail_at == "cursor":
            raise RuntimeError("cursor write failed")
        self.advanced = True
        self.cursor.cursor_value = kwargs["cursor_after"]
        self.cursor.version += 1
        return True

    async def finalize_acquisition_run(self, **kwargs: Any) -> Any:
        self.events.append(f"run_finalize:{kwargs['status']}")
        self.finalized_status = kwargs["status"]
        return self.run


class FakeObjectStore:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at

    def put_bytes(self, key: str, data: bytes) -> str:
        self.events.append("object_write")
        if self.fail_at == "object":
            raise OSError("object write failed")
        return f"memory://{key}"


class FakeMaterializer:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at

    def materialize(self, page: AuthorizedSourcePage, **kwargs: Any) -> Any:
        self.events.append("materialize")
        if self.fail_at == "materialize":
            raise BundleQuarantineError("invalid_bundle", "synthetic quarantine")
        return SimpleNamespace(
            path=Path("C:/synthetic/ccgp-live-bundle"),
            bundle_id="ccgp-live-bundle",
            response_sha256=page.response_sha256,
        )


class FakeImporter:
    def __init__(self, events: list[str], fail_at: str | None) -> None:
        self.events = events
        self.fail_at = fail_at

    async def import_bundle(self, path: Path) -> Any:
        self.events.append("import")
        if self.fail_at == "import":
            raise RuntimeError("import failed")
        return SimpleNamespace(metrics={"notice_count": 1}, _reprocessing="new")


@pytest.mark.asyncio
async def test_run_once_orders_acquire_store_materialize_import_then_cursor_commit() -> None:
    events: list[str] = []
    repository = FakeRepository(events, None)
    service = IngestionService(
        source_client=FakeSourceClient(
            [_page("cursor-1", "cursor-2", "n-1"), _page("cursor-2", None, "n-2")],
            events,
            None,
        ),
        acquisition_repository=repository,
        object_store=FakeObjectStore(events, None),
        materializer=FakeMaterializer(events, None),
        importer=FakeImporter(events, None),
        data_contract=_contract(),
        batch_id="ccgp-batch-20260730",
        clock=FixedClock(FIXED_NOW),
        max_pages_per_run=2,
        min_interval_seconds=1,
        sleep=lambda _seconds: _record_sleep(events),
        commit=lambda: _record_commit(events),
        audit=lambda _details: _record_audit(events),
    )

    result = await service.run_once()

    assert result.status == "success"
    assert repository.advanced is True
    assert events.index("acquire:cursor-1") < events.index("object_write")
    assert events.index("object_write") < events.index("materialize")
    assert events.index("materialize") < events.index("import")
    assert events.index("import") < events.index("cursor_advance")
    assert events.index("cursor_advance") < events.index("audit") < events.index("commit")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["acquire", "object", "materialize", "import", "cursor"])
async def test_run_once_failures_do_not_advance_cursor_and_are_recorded(fail_at: str) -> None:
    events: list[str] = []
    repository = FakeRepository(events, fail_at)
    service = IngestionService(
        source_client=FakeSourceClient(
            [_page("cursor-1", "cursor-2", "n-1"), _page("cursor-2", None, "n-2")],
            events,
            fail_at,
        ),
        acquisition_repository=repository,
        object_store=FakeObjectStore(events, fail_at),
        materializer=FakeMaterializer(events, fail_at),
        importer=FakeImporter(events, fail_at),
        data_contract=_contract(),
        batch_id="ccgp-batch-20260730",
        clock=FixedClock(FIXED_NOW),
        max_pages_per_run=2,
        min_interval_seconds=1,
        sleep=lambda _seconds: _record_sleep(events),
        commit=lambda: _record_commit(events),
        audit=lambda _details: _record_audit(events),
    )

    result = await service.run_once()

    assert result.status in {"failed", "quarantined"}
    assert repository.advanced is False
    assert repository.finalized_status in {"failed", "quarantined"}
    assert "commit" in events


@pytest.mark.asyncio
async def test_run_once_honors_page_limit_without_advancing_cursor() -> None:
    events: list[str] = []
    repository = FakeRepository(events, None)
    service = IngestionService(
        source_client=FakeSourceClient(
            [_page("cursor-1", "cursor-2", "n-1"), _page("cursor-2", None, "n-2")],
            events,
            None,
        ),
        acquisition_repository=repository,
        object_store=FakeObjectStore(events, None),
        materializer=FakeMaterializer(events, None),
        importer=FakeImporter(events, None),
        data_contract=_contract(),
        batch_id="ccgp-batch-20260730",
        clock=FixedClock(FIXED_NOW),
        max_pages_per_run=1,
        min_interval_seconds=1,
        sleep=lambda _seconds: _record_sleep(events),
        commit=lambda: _record_commit(events),
        audit=lambda _details: _record_audit(events),
    )

    result = await service.run_once()

    assert result.status == "quarantined"
    assert result.request_count == 1
    assert repository.advanced is False
    assert "sleep" not in events


@pytest.mark.asyncio
async def test_run_once_records_bounded_rate_limit_without_advancing_cursor() -> None:
    events: list[str] = []
    repository = FakeRepository(events, None)
    service = IngestionService(
        source_client=RateLimitedSourceClient(),
        acquisition_repository=repository,
        object_store=FakeObjectStore(events, None),
        materializer=FakeMaterializer(events, None),
        importer=FakeImporter(events, None),
        data_contract=_contract(),
        batch_id="ccgp-batch-20260730",
        clock=FixedClock(FIXED_NOW),
        max_pages_per_run=2,
        min_interval_seconds=1,
        sleep=lambda _seconds: _record_sleep(events),
        commit=lambda: _record_commit(events),
        audit=lambda _details: _record_audit(events),
    )

    result = await service.run_once()

    assert result.status == "rate_limited"
    assert result.failure_code == "rate_limited"
    assert result.retry_after_seconds == 30
    assert repository.advanced is False

async def _record_sleep(events: list[str]) -> None:
    events.append("sleep")


async def _record_commit(events: list[str]) -> None:
    events.append("commit")


async def _record_audit(events: list[str]) -> None:
    events.append("audit")
