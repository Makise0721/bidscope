from logging.config import fileConfig

from alembic import context
from bidscope.config import get_settings
from bidscope.persistence.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the database URL from settings for both modes.

    Reading from settings (BIDSCOPE_CHECKPOINT_DATABASE_URL) — rather than the
    static sqlalchemy.url in alembic.ini — ensures Alembic targets the same
    database the test or deployment environment configures. A wrong or missing
    URL raises immediately instead of silently falling back to a default.
    """
    url = get_settings().checkpoint_database_dsn()
    if not url:
        raise RuntimeError(
            "BIDSCOPE_CHECKPOINT_DATABASE_URL is not set; "
            "Alembic cannot determine which database to migrate."
        )
    return url


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=lambda obj, name, type_, *args: _include_schema_only(name, type_),
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _include_schema_only(name: str | None, type_: str) -> bool:
    if type_ == "table":
        return name in target_metadata.tables
    return True


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=_database_url(),
        # A short connect_timeout makes an unreachable checkpoint URL fail fast
        # rather than retrying for a long time. The default (no timeout) would
        # also work but slows down connection-failure scenarios.
        connect_args={"connect_timeout": 5},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=lambda obj, name, type_, *args: _include_schema_only(name, type_),
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
