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
_ALLOWED_POSTGRES_SCHEMES = frozenset(
    {"postgresql", "postgresql+asyncpg", "postgresql+psycopg"}
)
_ALLOWED_TEST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
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
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<redacted PostgreSQL URL>"

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_POSTGRES_SCHEMES or not url.startswith(f"{scheme}://"):
        return "<redacted PostgreSQL URL>"

    try:
        port = parsed.port or _POSTGRES_DEFAULT_PORT
        database = _database_name(url)
    except ValueError:
        return "<redacted PostgreSQL URL>"

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
    """Return validated (host, port, database) for a local PostgreSQL URL."""
    raw_scheme, separator, _ = url.partition("://")
    if not separator or raw_scheme not in _ALLOWED_POSTGRES_SCHEMES:
        return None

    try:
        parsed = urlparse(url)
        if parsed.scheme != raw_scheme or not parsed.netloc:
            return None
        host = parsed.hostname
        if host is None or host.casefold() not in _ALLOWED_TEST_HOSTS:
            return None

        authority = parsed.netloc.rsplit("@", 1)[-1]
        if authority.startswith("["):
            closing_bracket = authority.find("]")
            if closing_bracket < 0:
                return None
            port_text = authority[closing_bracket + 1 :]
            if port_text and (not port_text.startswith(":") or not port_text[1:]):
                return None
        elif ":" in authority:
            port_text = authority.rsplit(":", 1)[1]
            if not port_text.isdigit():
                return None
        else:
            port_text = ""

        port = parsed.port
        if port is None:
            if port_text:
                return None
            port = _POSTGRES_DEFAULT_PORT
        if not 1 <= port <= 65535:
            return None

        if not parsed.path.startswith("/") or parsed.path.count("/") != 1:
            return None
        database = parsed.path[1:]
        if (
            not database
            or not _SAFE_DATABASE_NAME.fullmatch(database)
            or parsed.params
            or parsed.fragment
        ):
            return None
    except ValueError:
        return None

    return (host.casefold(), port, database)


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
        del database_name
        pytest.fail(
            "database URL must target a dedicated test database (name must end with "
            "'_test' or '_e2e')"
        )

    checkpoint_name = _database_name(checkpoint_url)
    if not checkpoint_name or not re.search(r"_(test|e2e)$", checkpoint_name):
        del database_url
        del checkpoint_url
        del settings
        del database_name
        del checkpoint_name
        pytest.fail(
            "checkpoint URL must target a dedicated test database (name must end with "
            "'_test' or '_e2e')"
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
