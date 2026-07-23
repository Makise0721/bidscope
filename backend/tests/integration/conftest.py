import asyncio
import os
import subprocess
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.config import get_settings
from bidscope.graph.executor import run_setup_checkpoints
from bidscope.testing import enforce_test_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# asyncpg is incompatible with the default Windows ProactorEventLoop.
# The documented workaround is to use the Selector event loop policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_owned_event_loops: set[asyncio.AbstractEventLoop] = set()


def _current_event_loop() -> asyncio.AbstractEventLoop | None:
    """Return the policy loop without emitting Python's absent-loop warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            return None


def _ensure_current_event_loop() -> asyncio.AbstractEventLoop:
    """Install and track a usable loop after sync helpers clear the policy loop."""
    loop = _current_event_loop()
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _owned_event_loops.add(loop)
    return loop


def _close_owned_event_loops() -> None:
    """Close only loops created by this integration harness."""
    current_loop = _current_event_loop()
    current_loop_was_owned = False
    close_failures: list[BaseException] = []
    for loop in tuple(_owned_event_loops):
        current_loop_was_owned |= loop is current_loop
        try:
            if not loop.is_closed():
                loop.close()
        except BaseException as exc:
            close_failures.append(exc)
        finally:
            _owned_event_loops.discard(loop)

    if current_loop_was_owned:
        asyncio.set_event_loop(None)

    if close_failures:
        raise BaseExceptionGroup(
            "Failed to close one or more harness event loops", close_failures
        )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Repair the policy loop before pytest-asyncio prepares an async test."""
    _ = item
    _ensure_current_event_loop()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> None:
    """Close harness loops after all per-test fixture finalizers have run."""
    _ = item, nextitem
    _close_owned_event_loops()


def _restore_event_loop(
    pytest_asyncio_loop: asyncio.AbstractEventLoop,
) -> None:
    """Restore pytest-asyncio's loop during active test execution."""
    if pytest_asyncio_loop.is_closed():
        raise RuntimeError(
            "pytest-asyncio session event loop is closed during active test execution"
        )
    asyncio.set_event_loop(pytest_asyncio_loop)


# Keep this private dependency: pytest-asyncio 0.26's fixture cleanup ordering
# is part of the event-loop isolation contract tested by this harness.
@pytest.fixture(autouse=True)
def _restore_event_loop_around_integration_test(
    _session_event_loop: asyncio.AbstractEventLoop,
) -> Iterator[None]:
    """Keep pytest-asyncio's loop current across sync and async tests."""
    _restore_event_loop(_session_event_loop)
    yield
    _restore_event_loop(_session_event_loop)


@pytest.fixture(scope="session", autouse=True)
def _close_harness_event_loops(
    _session_event_loop: asyncio.AbstractEventLoop,
) -> Iterator[None]:
    """Close residual harness loops before pytest-asyncio tears down its loop."""
    _ = _session_event_loop
    yield
    _close_owned_event_loops()


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session", autouse=True)
def _enforce_test_environment() -> None:
    """Fail-closed guard: integration tests must run against a dedicated test database."""
    enforce_test_environment()


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations(_enforce_test_environment: None) -> None:
    """Run Alembic via an isolated synchronous subprocess (psycopg driver).

    Keeping migrations in a separate process guarantees no async connection
    shares an event loop with the test session. The subprocess inherits the
    current environment so it reads the same BIDSCOPE_CHECKPOINT_DATABASE_URL
    as the test suite, ensuring migrations target the test database.
    """
    env = os.environ.copy()
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )


@pytest.fixture(scope="session", autouse=True)
def _setup_checkpoint_schema(_apply_migrations: None) -> None:
    """Provision LangGraph tables explicitly before API/graph integration tests."""
    run_setup_checkpoints(get_settings())


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> sa.ext.asyncio.AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url)


@pytest.fixture
def session_factory(
    db_engine: sa.ext.asyncio.AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Provide test isolation on the shared test database.

    Integration tests run against a single Compose test database, so tables must
    be truncated before each test. CASCADE clears dependent tables
    (notice_versions, report_items, ...) that foreign-key into the truncated
    ones.
    """
    async with session_factory() as session:
        # query_runs CASCADEs to run_events and reports; the remaining tables
        # CASCADE among themselves via their foreign keys.
        await session.execute(
            sa.text(
                "TRUNCATE TABLE source_notices, canonical_notices, query_runs CASCADE"
            )
        )
        await session.commit()
