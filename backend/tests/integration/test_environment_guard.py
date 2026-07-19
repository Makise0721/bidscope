"""Regression tests for the fail-closed test-environment guard.

These tests assert that the integration suite refuses to run unless the
environment is explicitly configured as a dedicated test database. The guard
logic is imported and exercised directly via ``bidscope.testing`` so the tests
stay fast and deterministic.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from bidscope.testing import enforce_test_environment

# pytest.fail() raises this specific exception class; naming it keeps the
# ``pytest.raises`` checks from being blind (avoids ruff's B017).
_GUARD_FAILURE = pytest.fail.Exception


def _call_guard_with_env(overrides: dict[str, str]) -> None:
    """Invoke the real guard function under a patched environment."""
    from bidscope.config import get_settings

    # The settings singleton is cached with @lru_cache; clear it so the patched
    # environment variables take effect for the guard's get_settings() call.
    get_settings.cache_clear()
    try:
        with mock.patch.dict(os.environ, overrides, clear=False):
            enforce_test_environment()
    finally:
        get_settings.cache_clear()


def test_non_test_app_mode_is_rejected() -> None:
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {"BIDSCOPE_APP_MODE": "development", "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"}
        )


def test_non_test_database_is_rejected() -> None:
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {"BIDSCOPE_APP_MODE": "test", "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"}
        )


def test_valid_test_environment_is_accepted() -> None:
    # Should not raise.
    _call_guard_with_env(
        {
            "BIDSCOPE_APP_MODE": "test",
            "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test",
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test",
        }
    )


def test_fail_closed_message_mentions_required_suffix() -> None:
    """The rejection message must tell the user what database name is required."""
    from bidscope.config import get_settings

    get_settings.cache_clear()
    try:
        with mock.patch.dict(
            os.environ,
            {"BIDSCOPE_APP_MODE": "test", "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"},
            clear=False,
        ):
            with pytest.raises(_GUARD_FAILURE) as error:
                enforce_test_environment()
            assert "_test" in str(error.value) or "_e2e" in str(error.value)
    finally:
        get_settings.cache_clear()
