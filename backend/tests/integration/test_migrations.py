import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from bidscope.config import get_settings
from sqlalchemy.engine import make_url

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
        "report_claims",
        "report_claim_citations",
        "report_citations",
        "subscriptions",
        "subscription_seen_items",
        "inbox_events",
        "eval_cases",
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


async def test_alembic_uses_settings_checkpoint_url() -> None:
    """Alembic must follow BIDSCOPE_CHECKPOINT_DATABASE_URL, not a hardcoded default.

    With an unreachable checkpoint URL the command must fail rather than
    silently migrating a different (e.g. development) database.
    """
    env = os.environ.copy()
    guarded_checkpoint_url = make_url(get_settings().checkpoint_database_dsn()).set(port=65432)
    env["BIDSCOPE_CHECKPOINT_DATABASE_URL"] = guarded_checkpoint_url.render_as_string(
        hide_password=False
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


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_report_run_id_preflight_locks_reports_before_duplicate_query() -> None:
    """The preflight and constraint DDL must share a write-blocking table lock."""
    source = (
        PROJECT_ROOT
        / "migrations"
        / "versions"
        / "d8f4a9c2e6b1_complete_report_delivery_persistence.py"
    ).read_text(encoding="utf-8")

    lock_statement = 'op.execute("LOCK TABLE reports IN SHARE ROW EXCLUSIVE MODE")'
    duplicate_query = '"SELECT run_id::text AS run_id, count(*) AS report_count "'

    assert lock_statement in source
    assert source.index(lock_statement) < source.index(duplicate_query)


async def test_report_run_id_uniqueness_preflight_is_actionable(
    db_engine: sa.ext.asyncio.AsyncEngine,
) -> None:
    """Legacy duplicate run IDs must fail before the unique constraint is created."""
    env = os.environ.copy()
    downgrade = _alembic(env, "downgrade", "a1b2c3d4e5f6")
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    try:
        async with db_engine.begin() as connection:
            run_id = "00000000-0000-0000-0000-000000000123"
            await connection.execute(
                sa.text(
                    "INSERT INTO query_runs (id, run_key, status, user_request) "
                    "VALUES (:run_id, 'legacy-duplicate-run', 'completed', 'migration test')"
                ),
                {"run_id": run_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO reports (run_id, export_key, generated_at) VALUES "
                    "(:run_id, 'legacy-duplicate-one', now()), "
                    "(:run_id, 'legacy-duplicate-two', now())"
                ),
                {"run_id": run_id},
            )

        upgrade = _alembic(env, "upgrade", "head")
        output = upgrade.stdout + upgrade.stderr
        assert upgrade.returncode != 0
        assert "duplicate non-null reports.run_id values" in output
        assert run_id in output
        assert "resolve duplicate reports before retrying this migration" in output.lower()
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(
                sa.text("TRUNCATE TABLE source_notices, canonical_notices, query_runs CASCADE")
            )
        upgrade = _alembic(env, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr

    downgrade = _alembic(env, "downgrade", "a1b2c3d4e5f6")
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    upgrade = _alembic(env, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr


async def test_idempotency_keys_are_non_null_without_random_default(
    db_engine: sa.ext.asyncio.AsyncEngine,
) -> None:
    """Idempotency columns must be non-nullable and have no server default.

    The application is responsible for deriving a semantic idempotency key;
    a random default would mask missing-key bugs and would not be unique in
    practice. The ORM and the migrated schema must agree on both points.
    """
    async with db_engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT table_name, column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name IN "
                "('idempotency_key', 'run_key', 'export_key', 'trigger_key')"
            )
        )
        found = {(row[0], row[1]): (row[2], row[3]) for row in rows}
    expected = {
        ("snapshot_imports", "idempotency_key"),
        ("query_runs", "run_key"),
        ("reports", "export_key"),
        ("subscriptions", "trigger_key"),
    }
    assert expected <= set(found.keys()), f"missing idempotency columns: {found}"
    for key, (is_nullable, column_default) in found.items():
        assert is_nullable == "NO", f"{key} must be non-nullable"
        assert column_default is None, (
            f"{key} must have no server default, got {column_default!r}"
        )
