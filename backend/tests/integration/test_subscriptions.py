"""Integration tests for incremental tender subscriptions (Task 4: real execution).

A confirmed, scheduled subscription is stored with a next-run time. On a
subscription run the service executes the *real* graph (through the injected
:class:`~bidscope.api.dependencies.RunService`) for the persisted intent, gates
on the persisted online report, and only then diffs the report items against
the subscription's seen items:

* notices never seen before → ``new_notice`` inbox event.
* notices whose material fields changed since last seen →
  ``material_change`` event (formatting-only differences are ignored).
* unchanged notices → no event.

The seen-item cursor only advances after the run's report commits. Three
consecutive failures pause the subscription.

These tests run against the Compose test database with the demo bundle imported,
so retrieval returns real (synthetic-demo) notices for the graph to synthesize
into a report.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.api.dependencies import RunService, build_demo_graph
from bidscope.clock import FixedClock
from bidscope.config import Settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore
from bidscope.graph.executor import _to_plain_dsn
from bidscope.persistence.models import (
    InboxEvent,
    QueryRun,
    Subscription,
    SubscriptionSeenItem,
)
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SCHEDULED_QUERY = (
    "每周一上午 9 点，汇总近 7 天四川和重庆与「智算中心、服务器」有关、"
    "预算 500 万以上的招标信息。"
)
NON_SCHEDULED_QUERY = "查询四川省最近的服务器招标信息。"
TEST_DB_URL = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
TEST_CHECKPOINT_URL = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test"

BATCH_1 = Path("data/demo/batch-1")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _next_id(name: str) -> str:
    """Generate a deterministic UUID for a named test subscription."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_mode="test",
        database_url=TEST_DB_URL,
        checkpoint_database_url=TEST_CHECKPOINT_URL,
        real_model_enabled=False,
        admin_token="test-admin-token",
        object_store_root=str(tmp_path / "objects"),
        test_control_token="test-controls-token",
    )


async def _import_batch_1(tmp_path: Path) -> None:
    """Import batch-1 so the graph retrieves real (synthetic-demo) notices."""
    _, session_factory = create_engine_and_session()
    async with session_factory() as session:
        await session.execute(sa.text(
            "TRUNCATE TABLE snapshot_imports, snapshot_bundles, "
            "subscriptions, subscription_seen_items, inbox_events CASCADE"
        ))
        await session.commit()
    importer = SnapshotImporter(
        session_factory=session_factory,
        repository_factory=SnapshotRepository,
        object_store=LocalObjectStore(tmp_path / "snapshots"),
        clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
    )
    await importer.import_bundle(PROJECT_ROOT / BATCH_1)


async def _seed_completed_run(
    run_service: RunService,
    *,
    user_request: str = SCHEDULED_QUERY,
) -> str:
    """Drive a scheduled query through awaiting-confirmation to completion.

    Returns the run id of a completed, confirmed, scheduled run that is eligible
    to seed a subscription via :meth:`SubscriptionService.create_from_run`.
    """
    run_id, created = await run_service.create_run(user_request)
    assert created
    first = await run_service.execute_run(run_id, {"user_request": user_request})
    # A scheduled query always routes through the ``confirm_intent`` interrupt
    # before retrieval/delivery, so the first execution pauses here. Pin the
    # expected path so a future change that silently completes (or fails)
    # without the interrupt surfaces loudly.
    assert first.get("status") == "awaiting_confirmation", first
    await run_service.confirm(run_id)
    async with run_service.session_factory() as session:
        row = await session.get(QueryRun, run_id)
        assert row is not None
        assert row.status == "completed", row.status
    return run_id


class FailingReportRunService:
    """A RunService stand-in whose execution never produces a persisted report.

    Used to assert that a missing report is treated as a failure and does not
    advance the seen-item cursor.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def execute_run(
        self, run_id: str, input: Any, *, force_fresh: bool = False,
    ) -> dict[str, Any]:
        del run_id, input, force_fresh
        return {"status": "completed"}

    async def create_run(
        self, user_request: str, *, run_key: str | None = None,
    ) -> tuple[str, bool]:
        del user_request, run_key
        return str(uuid.uuid4()), True


async def count_seen_items(
    session_factory: async_sessionmaker[AsyncSession], sub_id: str,
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count()).select_from(SubscriptionSeenItem).where(
                SubscriptionSeenItem.subscription_id == sub_id
            )
        )
        return int(result.scalar_one())


async def count_inbox_events(
    session_factory: async_sessionmaker[AsyncSession], sub_id: str,
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count()).select_from(InboxEvent).where(
                InboxEvent.subscription_id == sub_id
            )
        )
        return int(result.scalar_one())


@pytest_asyncio.fixture()
async def run_service(tmp_path: Path) -> Any:
    """A real RunService + checkpointer; imports batch-1 first."""
    engine, session_factory = create_engine_and_session()
    await _import_batch_1(tmp_path)
    settings = _settings(tmp_path)
    object_store = LocalObjectStore(root=settings.object_store_root)
    dsn = _to_plain_dsn(settings.checkpoint_database_dsn())
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        graph = build_demo_graph(
            session_factory,
            settings,
            checkpointer=checkpointer,
            clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
            object_store=object_store,
        )
        service = RunService(
            session_factory, graph, object_store, settings,
            clock=FixedClock(datetime(2026, 7, 18, 9, 0, tzinfo=UTC)),
        )
        yield service
    await engine.dispose()


# ----------------------------------------------------- subscription creation ---


@pytest.mark.asyncio
async def test_subscription_requires_completed_confirmed_scheduled_run(
    run_service: RunService,
) -> None:
    """Creation contract: 404 for a missing run, 409 for an unconfirmed /
    unscheduled run, 201 for a completed confirmed scheduled run that
    materializes a real subscription.
    """
    from bidscope.subscriptions.service import SubscriptionService

    service = SubscriptionService(
        session_factory=run_service.session_factory, run_service=run_service,
    )

    # Missing run → raises a 404-shaped error.
    missing_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(LookupError):
        await service.create_from_run(missing_id)

    # A non-scheduled query never pauses and ends up completed without a
    # schedule: creation must be rejected.
    non_scheduled_id, _ = await run_service.create_run(NON_SCHEDULED_QUERY)
    await run_service.execute_run(
        non_scheduled_id, {"user_request": NON_SCHEDULED_QUERY},
    )
    with pytest.raises(ValueError):
        await service.create_from_run(non_scheduled_id)

    # A completed, confirmed, scheduled run is materialized as a real subscription.
    scheduled_run_id = await _seed_completed_run(run_service)
    sub = await service.create_from_run(scheduled_run_id)
    assert sub.status == "active"
    intent = sub.normalized_intent
    assert intent.get("__source_run_id") == scheduled_run_id
    assert intent.get("__user_request")
    assert intent.get("schedule")
    next_run_raw = intent.get("__next_run_at")
    assert next_run_raw
    next_run = datetime.fromisoformat(next_run_raw)
    assert next_run.tzinfo is not None


# ------------------------------------------------------- execution + report ---


@pytest.mark.asyncio
async def test_first_run_emits_new_notice_events_for_report_items(
    run_service: RunService,
) -> None:
    """The first scheduled run executes the real graph, gates on the persisted
    report, and emits a ``new_notice`` inbox event per report item."""
    from bidscope.subscriptions.service import SubscriptionService

    service = SubscriptionService(
        session_factory=run_service.session_factory, run_service=run_service,
    )
    scheduled_run_id = await _seed_completed_run(run_service)
    sub = await service.create_from_run(scheduled_run_id)

    stats = await service.run_subscription(sub.id)
    assert stats["failed"] is False
    assert stats["new_notices"] > 0
    assert stats["material_changes"] == 0

    assert await count_seen_items(run_service.session_factory, sub.id) == stats["new_notices"]
    inbox = await count_inbox_events(run_service.session_factory, sub.id)
    assert inbox == stats["new_notices"]


@pytest.mark.asyncio
async def test_seen_cursor_is_unchanged_when_report_persistence_fails(
    run_service: RunService,
) -> None:
    """A scheduled run whose report never commits leaves seen items and inbox
    events untouched and reports the run as failed."""
    from bidscope.subscriptions.service import KEY_CONSECUTIVE_FAILURES, SubscriptionService

    failing_service = SubscriptionService(
        session_factory=run_service.session_factory,
        run_service=FailingReportRunService(run_service.session_factory),
    )
    scheduled_run_id = await _seed_completed_run(run_service)
    sub = await failing_service.create_from_run(scheduled_run_id)
    before_failures = sub.normalized_intent.get(KEY_CONSECUTIVE_FAILURES)

    stats = await failing_service.run_subscription(sub.id)

    assert stats["failed"] is True
    assert stats["new_notices"] == 0
    assert stats["material_changes"] == 0
    assert await count_seen_items(run_service.session_factory, sub.id) == 0
    assert await count_inbox_events(run_service.session_factory, sub.id) == 0
    async with run_service.session_factory() as session:
        after = await session.get(Subscription, sub.id)
    assert after is not None
    assert after.normalized_intent[KEY_CONSECUTIVE_FAILURES] == before_failures + 1


def test_formatting_only_version_change_does_not_emit_material_change() -> None:
    """A pure content-hash change (no material field change) is not a
    ``material_change``.

    ``detect_material_changes`` is the pure function the subscription bridge
    calls when a notice's hash differs from its previously seen version. This
    pins the formatting-only contract at the unit level: identical deadline,
    budget, region, purchaser, scope, cancellation and evidence must yield no
    material-change entries.
    """
    from bidscope.retrieval.deduplication import NoticeView, detect_material_changes

    deadline = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    old_view = NoticeView(
        source="synthetic_demo",
        external_id="demo-001",
        canonical_url="https://example.invalid/1",
        project_number="P-001",
        content_hash="hash-old",
        title="服务器招标",
        purchaser="四川省财政厅",
        region="四川",
        budget_minor_units=5_000_000,
        budget_currency="CNY",
        deadline=deadline,
        claim_supporting_texts=("采购 100 台服务器",),
    )
    # Identical material fields, only the content_hash differs (formatting-only).
    new_view = NoticeView(
        source=old_view.source,
        external_id=old_view.external_id,
        canonical_url=old_view.canonical_url,
        project_number=old_view.project_number,
        content_hash="hash-new-formatted",
        title=old_view.title,
        purchaser=old_view.purchaser,
        region=old_view.region,
        budget_minor_units=old_view.budget_minor_units,
        budget_currency=old_view.budget_currency,
        deadline=old_view.deadline,
        claim_supporting_texts=old_view.claim_supporting_texts,
    )

    changes = detect_material_changes(old_view, new_view)
    assert changes == [], (
        "a formatting-only content-hash change must not be a material change"
    )

    # Sanity check: a real budget change does produce a material change.
    bumped_budget = NoticeView(
        **{**new_view.__dict__, "budget_minor_units": 6_000_000},
    )
    material = detect_material_changes(new_view, bumped_budget)
    assert any(change.field == "budget" for change in material)


@pytest.mark.asyncio
async def test_pending_run_can_be_resumed_by_execute_run(
    run_service: RunService,
) -> None:
    """A run left in ``pending`` after a pre-execution crash completes when
    re-driven by :meth:`execute_run`.

    P2-a decision record: ``_run_locked`` only drives ``created=True`` runs and
    relies on stale-run-recovery for stuck scheduled runs (see that method's
    docstring). This test pins the lower-level fact the decision rests on:
    that ``execute_run`` *does* resume a ``pending`` run, so the pending leg of
    a future state-dispatched recovery would be sound. Scoped to ``pending``
    (the short-lived state between ``create_run`` and ``execute_run``); the
    durable recovery path for ``retryable``/``awaiting_confirmation`` runs is
    ``mark_stale_runs_retryable`` + :meth:`RunService.retry` / the API confirm
    path, intentionally not the subscription tick.
    """
    pending_run_id, created = await run_service.create_run(
        SCHEDULED_QUERY, run_key=f"probe-pending:{uuid.uuid4()}",
    )
    assert created
    async with run_service.session_factory() as session:
        row = await session.get(QueryRun, pending_run_id)
    assert row is not None
    assert row.status == "pending"

    result = await run_service.execute_run(
        pending_run_id, {"user_request": SCHEDULED_QUERY},
    )
    if result.get("status") == "awaiting_confirmation":
        result = await run_service.confirm(pending_run_id)
    assert result.get("status") == "completed", result
    async with run_service.session_factory() as session:
        row = await session.get(QueryRun, pending_run_id)
    assert row is not None
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_corrupted_intent_surfaces_as_409(
    run_service: RunService, tmp_path: Path,
) -> None:
    """A completed run whose ``search_intent`` JSONB is structurally invalid
    must surface as a 409 (semantic), not escape as a 500.

    The route layer maps :class:`SubscriptionIntentError` to HTTP 409; a raw
    pydantic :class:`ValidationError` from :meth:`_resolve_intent` would escape
    as a 500. This test seeds a row directly (bypassing the validating create
    flow) so it represents the genuine data-corruption scenario.
    """
    from bidscope.subscriptions.service import SubscriptionIntentError, SubscriptionService

    corrupted_run_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "corrupted-intent-run"))
    async with run_service.session_factory() as session:
        session.add(QueryRun(
            id=corrupted_run_id,
            run_key=f"corrupted:{corrupted_run_id}",
            status="completed",
            user_request="anything",
            # Missing required ``topics`` and a structurally broken schedule:
            # ``topics`` is required by ``SearchIntent``, and the schedule's
            # ``cron_expression`` is missing. Both are guaranteed to trigger a
            # ``ValidationError`` on ``model_validate`` while still being a
            # valid JSONB value Postgres accepts.
            search_intent={"regions": ["四川"], "schedule": {"timezone": "Asia/Shanghai"}},
        ))
        await session.commit()

    service = SubscriptionService(
        session_factory=run_service.session_factory, run_service=run_service,
    )
    # The corruption must surface as the semantic-error type that the route
    # maps to 409, never as a raw ``ValidationError`` (which would 500).
    with pytest.raises(SubscriptionIntentError):
        await service.create_from_run(corrupted_run_id)


@pytest.mark.asyncio
async def test_three_consecutive_failures_pause_subscription(
    run_service: RunService,
) -> None:
    """Three consecutive failures pause the subscription."""
    from bidscope.subscriptions.service import SubscriptionService

    service = SubscriptionService(
        session_factory=run_service.session_factory,
        run_service=FailingReportRunService(run_service.session_factory),
    )
    scheduled_run_id = await _seed_completed_run(run_service)
    sub = await service.create_from_run(scheduled_run_id)

    for _ in range(3):
        result = await service.run_subscription(sub.id)
        assert result["failed"] is True

    async with run_service.session_factory() as session:
        sub_after = await session.get(Subscription, sub.id)
    assert sub_after is not None
    assert sub_after.status == "paused"
