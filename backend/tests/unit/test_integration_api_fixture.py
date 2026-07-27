"""Regression coverage for integration API fixture database targeting."""

from __future__ import annotations

from pathlib import Path

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
