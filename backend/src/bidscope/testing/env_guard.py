"""Fail-closed environment guard for the integration test suite.

Raises unless the suite is pointed at a dedicated ``*_test`` / ``*_e2e`` database
with ``app_mode=test``, so a misconfigured run can never truncate development
or production data.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from bidscope.config import get_settings


def _database_name(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or "").lstrip("/").split("?", 1)[0]


def enforce_test_environment() -> None:
    """Refuse to run integration tests outside a dedicated test database."""
    settings = get_settings()
    if settings.app_mode != "test":
        pytest.fail(
            "Integration tests require BIDSCOPE_APP_MODE=test "
            f"(current app_mode={settings.app_mode!r}). "
            "Refusing to run against a non-test environment."
        )
    db_name = _database_name(settings.database_url)
    if not db_name or not re.search(r"_(test|e2e)$", db_name):
        pytest.fail(
            "Integration tests require a database whose name ends with '_test' or '_e2e' "
            f"(current database name={db_name!r}, url={settings.database_url!r}). "
            "Refusing to run against a non-test database."
        )
