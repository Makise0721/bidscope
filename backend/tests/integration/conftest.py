import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from bidscope.config import get_settings
from bidscope.testing import enforce_test_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# asyncpg is incompatible with the default Windows ProactorEventLoop.
# The documented workaround is to use the Selector event loop policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
        await session.execute(
            sa.text("TRUNCATE TABLE source_notices, canonical_notices CASCADE")
        )
        await session.commit()
