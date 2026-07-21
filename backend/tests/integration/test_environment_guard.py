"""Regression tests for the fail-closed test-environment guard.

These tests assert that the integration suite refuses to run unless the
environment is explicitly configured as a dedicated test database. The guard
logic is imported and exercised directly via ``bidscope.testing`` so the tests
stay fast and deterministic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from bidscope.testing import enforce_test_environment

_GUARD_FAILURE = pytest.fail.Exception

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _call_guard_with_env(overrides: dict[str, str]) -> None:
    """Invoke the real guard function under a patched environment."""
    from bidscope.config import get_settings

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


def test_checkpoint_url_must_also_be_test_database() -> None:
    """The Alembic checkpoint URL must point at a test database too."""
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {
                "BIDSCOPE_APP_MODE": "test",
                "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test",
                "BIDSCOPE_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope",
            }
        )


def test_mismatched_host_is_rejected() -> None:
    """database_url and checkpoint_database_url must point at the same physical database."""
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {
                "BIDSCOPE_APP_MODE": "test",
                "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test",
                "BIDSCOPE_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://bidscope:bidscope@otherhost:5432/bidscope_test",
            }
        )


def test_mismatched_port_is_rejected() -> None:
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {
                "BIDSCOPE_APP_MODE": "test",
                "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test",
                "BIDSCOPE_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://bidscope:bidscope@localhost:65432/bidscope_test",
            }
        )


def test_mismatched_database_name_is_rejected() -> None:
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_env(
            {
                "BIDSCOPE_APP_MODE": "test",
                "BIDSCOPE_DATABASE_URL": "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test",
                "BIDSCOPE_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://bidscope:bidscope@localhost:5432/other_test",
            }
        )


def test_valid_test_environment_is_accepted() -> None:
    # Should not raise. Different driver prefixes are allowed as long as the
    # resolved host/port/database agree.
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


def test_subprocess_mixed_urls_are_rejected() -> None:
    """End-to-end: a real pytest subprocess with mixed URLs is blocked before migrations run."""
    env = os.environ.copy()
    env["BIDSCOPE_APP_MODE"] = "test"
    env["BIDSCOPE_DATABASE_URL"] = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test"
    env["BIDSCOPE_CHECKPOINT_DATABASE_URL"] = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope"
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "backend/tests/integration/test_environment_guard.py", "-q", "--no-header"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, f"expected rejection, got:\n{result.stdout}\n{result.stderr}"
