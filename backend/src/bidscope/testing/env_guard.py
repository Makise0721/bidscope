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
_GUARD_FAILURE = "Integration test database URLs are invalid or mismatched."


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
            or parsed.query
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

    database_url = settings.database_dsn()
    checkpoint_url = settings.checkpoint_database_dsn()
    database_parts = _connection_parts(database_url)
    checkpoint_parts = _connection_parts(checkpoint_url)
    is_valid = (
        database_parts is not None
        and checkpoint_parts is not None
        and database_parts == checkpoint_parts
        and database_parts[2].endswith(("_test", "_e2e"))
    )
    if not is_valid:
        # Drop settings, raw URLs, and parsed targets before pytest formats the
        # exception; the fixed marker exposes no password, host, or database.
        del database_url
        del checkpoint_url
        del settings
        del database_parts
        del checkpoint_parts
        del is_valid
        pytest.fail(_GUARD_FAILURE)
