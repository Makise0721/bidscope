"""Regression coverage for integration API fixture database targeting."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from bidscope.config import Settings
from integration.api import conftest as api_fixtures


def test_api_fixture_uses_the_guarded_settings_database_urls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    guarded_settings = Settings(
        app_mode="test",
        database_url=(
            "postgresql+asyncpg://bidscope:fixture-password@localhost:65432/fixture_test"
        ),
        checkpoint_database_url=(
            "postgresql+psycopg://bidscope:fixture-checkpoint-password"
            "@localhost:65432/fixture_test"
        ),
    )
    monkeypatch.setattr(api_fixtures, "get_settings", lambda: guarded_settings)

    settings = api_fixtures._settings(mode="test", tmp_path=tmp_path)

    assert settings.database_dsn() == guarded_settings.database_dsn()
    assert settings.checkpoint_database_dsn() == guarded_settings.checkpoint_database_dsn()


def test_api_integration_fixtures_do_not_pin_a_default_database_target() -> None:
    api_test_root = Path(__file__).parents[1] / "integration" / "api"

    for fixture_or_api_test in api_test_root.glob("*.py"):
        source = fixture_or_api_test.read_text(encoding="utf-8")
        assert "localhost:5432/bidscope_test" not in source


@pytest.mark.parametrize(
    ("module_name", "factory_name"),
    (
        ("integration.test_subscriptions", "_settings"),
        ("integration.test_scheduler_lock", "_test_settings"),
        ("integration.test_completed_run_delivery", "_settings"),
    ),
)
def test_graph_integration_helpers_use_guarded_database_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    factory_name: str,
) -> None:
    guarded_settings = Settings(
        app_mode="test",
        database_url=(
            "postgresql+asyncpg://bidscope:fixture-password@localhost:65432/fixture_test"
        ),
        checkpoint_database_url=(
            "postgresql+psycopg://bidscope:fixture-checkpoint-password"
            "@localhost:65432/fixture_test"
        ),
    )
    module = import_module(module_name)
    monkeypatch.setattr(module, "get_settings", lambda: guarded_settings)

    settings = getattr(module, factory_name)(tmp_path)

    assert settings.database_dsn() == guarded_settings.database_dsn()
    assert settings.checkpoint_database_dsn() == guarded_settings.checkpoint_database_dsn()
