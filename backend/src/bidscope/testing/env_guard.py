"""Fail-closed environment guard for the integration test suite.

Raises unless the suite is pointed at a dedicated ``*_test`` / ``*_e2e`` database
with ``app_mode=test``, so a misconfigured run can never truncate development
or production data.

Both ``database_url`` (used by the async test session) *and*
``checkpoint_database_url`` (used by Alembic's synchronous migration subprocess)
are validated. A mismatch — for example ``database_url`` pointing at a test
database while ``checkpoint_database_url`` points at development — would let
migrations run against the wrong database, so we refuse unless both URLs agree
on host, port and database name (only the SQLAlchemy driver prefix may differ).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from bidscope.config import get_settings

_POSTGRES_DEFAULT_PORT = 5432
_SAFE_DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SAFE_IPV6_HOST = re.compile(r"[0-9A-Fa-f:.%]+\Z")


def _database_name(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return (parsed.path or "").lstrip("/").split("?", 1)[0]


def _sanitize_dsn(url: str) -> str:
    """Return PostgreSQL connection metadata without user-info or query data."""
    scheme, separator, _ = url.partition("://")
    if not separator or not scheme.startswith("postgresql"):
        return "<redacted PostgreSQL URL>"

    try:
        parsed = urlparse(url)
        port = parsed.port or _POSTGRES_DEFAULT_PORT
        database = _database_name(url)
    except ValueError:
        return f"{scheme}://<redacted>"

    if not _SAFE_DATABASE_NAME.fullmatch(database):
        database = "<redacted>"

    # Without an '@', the authority can be malformed user-info or a password
    # in a nonstandard location. Keep the scheme and database only.
    if "@" not in parsed.netloc:
        authority = "<redacted>"
    else:
        try:
            host = parsed.hostname or "<redacted>"
        except ValueError:
            host = "<redacted>"
        if ":" in host:
            safe_host = f"[{host}]" if _SAFE_IPV6_HOST.fullmatch(host) else "<redacted>"
        else:
            safe_host = host if _SAFE_HOST.fullmatch(host) else "<redacted>"
        authority = f"{safe_host}:{port}"

    return f"{scheme}://{authority}/{database}"


def _connection_parts(url: str) -> tuple[str, int, str] | None:
    """Return (host, port, database) for a ``postgresql`` URL.

    Alembic's sync driver (``postgresql+psycopg``) and the async test driver
    (``postgresql+asyncpg``) may use different driver prefixes yet point at the
    same physical database. We therefore compare host/port/database after
    stripping the driver, defaulting to the PostgreSQL standard port when none
    is specified.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or _POSTGRES_DEFAULT_PORT
        database = _database_name(url)
    except ValueError:
        return None
    return (host, port, database)


def enforce_test_environment() -> None:
    """Refuse to run integration tests outside a dedicated test database."""
    settings = get_settings()
    if settings.app_mode != "test":
        app_mode = settings.app_mode
        del settings
        pytest.fail(
            "Integration tests require BIDSCOPE_APP_MODE=test "
            f"(current app_mode={app_mode!r}). "
            "Refusing to run against a non-test environment."
        )

    database_url = settings.database_url
    checkpoint_url = settings.checkpoint_database_url

    database_name = _database_name(database_url)
    if not database_name or not re.search(r"_(test|e2e)$", database_name):
        del database_url
        del checkpoint_url
        del settings
        pytest.fail(
            "Integration tests require a database whose name ends with '_test' or '_e2e' "
            f"(current BIDSCOPE_DATABASE_URL database name={database_name!r}). "
            "Refusing to run against a non-test database."
        )

    checkpoint_name = _database_name(checkpoint_url)
    if not checkpoint_name or not re.search(r"_(test|e2e)$", checkpoint_name):
        del database_url
        del checkpoint_url
        del settings
        pytest.fail(
            "Integration tests require the Alembic checkpoint database name to end with "
            "'_test' or '_e2e' "
            f"(current BIDSCOPE_CHECKPOINT_DATABASE_URL database name={checkpoint_name!r}). "
            "Refusing to run migrations against a non-test database."
        )

    database_parts = _connection_parts(database_url)
    checkpoint_parts = _connection_parts(checkpoint_url)
    if database_parts is None or checkpoint_parts is None or database_parts != checkpoint_parts:
        sanitized_database_url = _sanitize_dsn(database_url)
        sanitized_checkpoint_url = _sanitize_dsn(checkpoint_url)
        # Drop settings and raw URLs before pytest formats the failure so the
        # retained traceback frame cannot expose credentials through locals.
        del database_url
        del checkpoint_url
        del settings
        del database_name
        del checkpoint_name
        del database_parts
        del checkpoint_parts
        pytest.fail(
            "Integration tests require BIDSCOPE_DATABASE_URL and "
            "BIDSCOPE_CHECKPOINT_DATABASE_URL to point at the same physical database "
            "(host, port and database name must match; only the SQLAlchemy driver prefix "
            "may differ). "
            f"database_url={sanitized_database_url!r}, "
            f"checkpoint_database_url={sanitized_checkpoint_url!r}. "
            "Refusing to run with mismatched database URLs."
        )
