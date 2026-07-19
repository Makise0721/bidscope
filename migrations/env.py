from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from bidscope.config import get_settings
from bidscope.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().checkpoint_database_url
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
