# BidScope Scheduler and Integration Test Isolation Design

**Date:** 2026-07-20
**Scope:** Resolve the two known issues recorded in the tasks 13-16 handoff:

1. API integration tests contaminate later async integration tests when multiple
   files run in one pytest process on Windows.
2. The APScheduler process role registers a one-minute job whose `_tick` body is
   still a no-op.

This work does not start task 17, evaluation, hardening, or deployment work.

## Problem Statement

The API integration suite uses a synchronous Starlette `TestClient`. Its
lifespan starts and disposes an async SQLAlchemy engine through an anyio portal.
On Windows, disposing the client can leave the main thread's event-loop policy
without a current loop. pytest-asyncio then fails before an async subscription
test is awaited with `RuntimeError: There is no current event loop in thread
'MainThread'`. Running the API and subscription suites separately hides the
problem.

The scheduler currently exposes a CLI one-shot implementation, but the
APScheduler callback `_tick` ignores its settings and never creates a session,
finds due subscriptions, or invokes `SubscriptionService`.

## Goals

- Make the documented API, subscription, and scheduler-lock integration tests
  pass together in one pytest process on Windows.
- Keep the existing synchronous API test contract and avoid rewriting the SSE
  tests.
- Ensure async database resources are used within a compatible event-loop
  lifetime in tests.
- Provide one scheduler tick implementation shared by the CLI one-shot command
  and the APScheduler process role.
- Filter subscriptions by their persisted internal next-run timestamp.
- Advance a subscription's next-run timestamp only after a successful or
  explicitly skipped run; retain an overdue timestamp after a failure so the
  next tick retries it.
- Add focused regression tests for loop restoration and scheduler behavior.
- Preserve the frozen persistence schema and the existing subscription API.

## Non-goals

- No task 17 operational views or evaluation runner.
- No database migration or new persistence columns.
- No switch from APScheduler to another job system.
- No public-source network access.
- No rewrite of the API tests to an asynchronous HTTP client.
- No change to the subscription graph simplification already documented as a
  P0 limitation.

## Design

### 1. Test event-loop ownership

The integration test conftest will establish an explicit Windows selector-loop
policy and add an autouse fixture that runs around each integration test:

- Before the test, inspect the current policy loop.
- If no loop exists or the loop is closed, create and install a new selector
  loop.
- Yield to the test.
- After the test, close only a loop owned by the fixture and restore a usable
  loop for the next pytest-asyncio test when a synchronous `TestClient` has
  cleared the policy loop.

The fixture will not close the session-scoped async engine. The existing shared
engine/session fixture remains aligned with the configured pytest-asyncio
session loop, so pooled asyncpg connections do not move between loops. API
clients that create their own app will continue to use `with TestClient(...)` so
FastAPI lifespan cleanup remains deterministic.

The regression gate is the combined command:

```bash
uv run pytest backend/tests/integration/api \
  backend/tests/integration/test_subscriptions.py \
  backend/tests/integration/test_scheduler_lock.py -q
```

The individual task gates remain required as well.

### 2. Scheduler data flow

The scheduler module will expose a single async core:

```text
run_scheduler_tick(settings, now=None)
  -> create engine/session factory
  -> list_due_subscriptions(session_factory, now)
  -> for each due subscription:
       SubscriptionService.run_subscription(subscription.id)
       if success or skipped: persist next __next_run_at
       if failed: retain current __next_run_at
  -> dispose engine
```

`list_due_subscriptions` will read active subscriptions and parse
`normalized_intent["__next_run_at"]`. A subscription is due when its timestamp
is less than or equal to the supplied/current UTC time. Missing or malformed
internal timestamps are ignored rather than crashing the entire tick; the
function will return only valid due records. The stored timestamp is interpreted
with its timezone offset. The existing `timezone` field is used when computing
the next cron occurrence.

The next occurrence is calculated from the subscription cron expression and the
current scheduled occurrence. The value is written back to the JSONB internal
key `__next_run_at` in a short transaction after the run result is known. A
successful run resets the existing failure counter through the service's
success path; a skipped run (another worker held the advisory lock) advances
its schedule because no retry is needed. A failed run leaves the overdue value
unchanged so a later tick can retry it. If the subscription was paused by the
service after repeated failures, it is no longer selected by the next tick.

The sync APScheduler callback `_tick(settings)` will call the async core using
`asyncio.run`. The CLI `scheduler run` command will call the same async core
instead of duplicating list-and-run logic. Both paths will dispose the engine in
`finally`, including error paths.

`run_subscription` will keep its existing result keys and add `skipped: bool` to
all result shapes. Existing callers and tests that inspect `failed` remain
compatible. A lock miss returns `skipped=True`; a real execution returns
`skipped=False`.

### 3. Error handling

- A malformed schedule state is isolated to that subscription and does not
  prevent other active subscriptions from running.
- A subscription run exception is recorded as a failed result for the tick and
  does not prevent the remaining due subscriptions from being attempted.
- Engine disposal occurs even when listing or running subscriptions raises.
- No scheduler path silently treats a failed run as successful or advances its
  retry point.
- Existing advisory locks continue to use the frozen SHA-256-derived signed
  64-bit key and session-level PostgreSQL lock semantics.

## Testing

Add focused tests for:

- The event-loop fixture restoring a current loop after a synchronous client
  lifecycle, plus the combined API/subscription/scheduler-lock command.
- Due filtering for past, present, future, missing, and malformed timestamps.
- A successful tick invoking due subscriptions and advancing their next-run
  timestamps.
- A failed tick retaining the overdue timestamp and continuing to the next
  subscription.
- A lock-skipped result being distinguishable from a successful execution.
- `build_scheduler` registering the real tick callback with the one-minute
  interval.

Verification commands:

```bash
export BIDSCOPE_APP_MODE=test
export BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
export BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test

uv run pytest backend/tests/integration/api -q
uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
uv run pytest backend/tests/integration/api backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
uv run pytest backend/tests/unit/subscriptions -q
uv run ruff check backend/src/bidscope/subscriptions backend/tests/integration backend/tests/unit/subscriptions
uv run mypy backend/src/bidscope/subscriptions
```

The combined command is the acceptance gate for the original cross-file
pollution issue. If database-backed scheduler tests are too environment-bound,
the due-state and tick orchestration tests will use a fake session factory while
the existing PostgreSQL advisory-lock integration test remains the source of
truth for lock behavior.

## Alternatives Considered

### Rewrite API and SSE tests to AsyncClient

This would remove the sync/async boundary and make lifespan ownership explicit,
but it would substantially rewrite stable API tests and add manual lifespan
plumbing. It is unnecessary for the current failure and increases P0 blast
radius.

### Change only pytest configuration to function-scoped loops

This is insufficient because the session-scoped database engine depends on the
session loop; it produces `ScopeMismatch` errors or cross-loop connection-pool
failures unless the fixture graph is redesigned. The explicit restoration
fixture preserves the existing resource model while addressing the actual
Windows failure.

### Leave scheduler state unfiltered and only invoke `run_subscription`

This would make the process role execute future subscriptions early and would
not provide a retryable schedule after failures. The tick must own due filtering
and next-run advancement to be operationally correct.

## Acceptance Criteria

- The combined integration command passes in a single pytest process on Windows.
- The three individual official task gates continue to pass.
- APScheduler's registered job invokes real subscription work.
- Future subscriptions are not run, due subscriptions are run once per tick,
  successful/skipped runs advance their schedule, and failed runs remain due.
- No migration changes are introduced.
- No existing source or checkpoint contract is modified.
