from __future__ import annotations

from unittest.mock import Mock

import pytest
from bidscope.config import Settings
from bidscope.db import create_engine_and_session


def test_create_engine_configures_postgres_pool_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_engine = Mock()
    monkeypatch.setattr("bidscope.db.create_async_engine", create_engine)
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost:5432/db",
        db_pool_size=7,
        db_max_overflow=3,
        db_pool_recycle_seconds=91,
        db_connect_timeout_seconds=12,
        db_command_timeout_seconds=34,
    )

    create_engine_and_session(settings)

    create_engine.assert_called_once_with(
        settings.database_dsn(),
        pool_size=7,
        max_overflow=3,
        pool_recycle=91,
        connect_args={"timeout": 12, "command_timeout": 34},
    )


def test_create_engine_does_not_apply_postgres_pool_options_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_engine = Mock()
    monkeypatch.setattr("bidscope.db.create_async_engine", create_engine)
    settings = Settings(database_url="sqlite+aiosqlite:///./test.db")

    create_engine_and_session(settings)

    create_engine.assert_called_once_with(settings.database_dsn())
