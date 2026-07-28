from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bidscope.config import Settings, get_settings


def create_engine_and_session(
    settings: Settings | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    resolved = settings or get_settings()
    dsn = resolved.database_dsn()
    engine_options: dict[str, object] = {}
    if dsn.startswith("postgresql+asyncpg://"):
        engine_options.update(
            pool_size=resolved.db_pool_size,
            max_overflow=resolved.db_max_overflow,
            pool_recycle=resolved.db_pool_recycle_seconds,
            connect_args={
                "timeout": resolved.db_connect_timeout_seconds,
                "command_timeout": resolved.db_command_timeout_seconds,
            },
        )
    engine = create_async_engine(dsn, **engine_options)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory
