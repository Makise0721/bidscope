import sqlalchemy as sa


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
