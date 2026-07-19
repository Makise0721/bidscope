import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def test_initial_migration_creates_core_tables(db_engine: sa.ext.asyncio.AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        names = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
    assert {
        "snapshot_bundles",
        "snapshot_imports",
        "source_notices",
        "notice_versions",
        "canonical_notices",
        "notice_evidence",
        "query_runs",
        "run_events",
        "reports",
        "report_items",
        "subscriptions",
        "subscription_seen_items",
        "inbox_events",
        "eval_runs",
    } <= set(names)


async def test_migration_enables_required_extensions(
    db_engine: sa.ext.asyncio.AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            sa.text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
        )
        extensions = {row[0] for row in result}
    assert {"vector", "pg_trgm"} <= extensions


def test_alembic_uses_settings_checkpoint_url() -> None:
    """Alembic must follow BIDSCOPE_CHECKPOINT_DATABASE_URL, not a hardcoded default.

    With an unreachable checkpoint URL the command must fail rather than
    silently migrating a different (e.g. development) database.
    """
    env = os.environ.copy()
    env["BIDSCOPE_CHECKPOINT_DATABASE_URL"] = (
        "postgresql+psycopg://bidscope:bidscope@localhost:65432/bidscope_test"
    )
    env.pop("BIDSCOPE_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, "alembic must fail when the checkpoint URL is unreachable"
    assert "65432" in combined or "connection" in combined.lower()
