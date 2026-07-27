"""Security regressions for test environment database URL failures."""

from __future__ import annotations

import os
import traceback
from collections.abc import Iterator
from unittest import mock

import pytest
from bidscope.testing import enforce_test_environment

_GUARD_FAILURE = pytest.fail.Exception

_CASES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "normal": (
        "postgresql+asyncpg://bidscope:raw-db-password@localhost:5432/bidscope_test",
        "postgresql+psycopg://bidscope:raw-checkpoint-password@127.0.0.1:5432/bidscope_test",
        ("raw-db-password", "raw-checkpoint-password"),
    ),
    "encoded": (
        "postgresql+asyncpg://bidscope:encoded-db-p%40ss%3Aword%2F%3F%23%5B%5D@localhost:5432/bidscope_test",
        "postgresql+psycopg://bidscope:encoded-checkpoint-p%40ss%3Aword%2F%3F%23%5B%5D@127.0.0.1:5432/bidscope_test",
        (
            "encoded-db-p@ss:word/?#[]",
            "encoded-db-p%40ss%3Aword%2F%3F%23%5B%5D",
            "encoded-checkpoint-p@ss:word/?#[]",
            "encoded-checkpoint-p%40ss%3Aword%2F%3F%23%5B%5D",
        ),
    ),
    "malformed": (
        "postgresql+asyncpg://bidscope:malformed-db-%ZZ@localhost:5432/bidscope_test",
        "postgresql+psycopg://bidscope:malformed-checkpoint-%ZZ@otherhost:5432/bidscope_test",
        ("malformed-db-%ZZ", "malformed-checkpoint-%ZZ"),
    ),
    "no-at": (
        "postgresql+asyncpg://no-at-db-password/bidscope_test",
        "postgresql+psycopg://no-at-checkpoint-password/bidscope_test",
        ("no-at-db-password", "no-at-checkpoint-password"),
    ),
    "malformed-scheme": (
        "postgresql+asyncpg:malformed-scheme-db-password://localhost:5432/bidscope_test",
        "postgresql+psycopg:malformed-scheme-checkpoint-password://otherhost:5432/bidscope_test",
        ("malformed-scheme-db-password", "malformed-scheme-checkpoint-password"),
    ),
    "encoded-malformed-scheme": (
        "postgresql+asyncpg:encoded-scheme-db-%70%61%73%73%77%6f%72%64://localhost:5432/bidscope_test",
        "postgresql+psycopg:encoded-scheme-checkpoint-%70%61%73%73%77%6f%72%64://otherhost:5432/bidscope_test",
        (
            "encoded-scheme-db-password",
            "encoded-scheme-checkpoint-password",
            "%70%61%73%73%77%6f%72%64",
        ),
    ),
    "unknown-scheme": (
        "postgresql+unknown-db-password://localhost:5432/bidscope_test",
        "postgresql+unknown-checkpoint-password://otherhost:5432/bidscope_test",
        ("postgresql+unknown-db-password", "postgresql+unknown-checkpoint-password"),
    ),
}


_VALID_DATABASE_URL = (
    "postgresql+asyncpg://bidscope:valid-password@localhost:5432/bidscope_test"
)
_VALID_CHECKPOINT_URL = (
    "postgresql+psycopg://bidscope:valid-checkpoint-password@localhost:5432/bidscope_test"
)

_MALFORMED_URLS: dict[str, str] = {
    "unsupported-scheme": (
        "postgresql+unknown://bidscope:unsupported-password@localhost:5432/bidscope_test"
    ),
    "missing-separator": (
        "postgresql+asyncpg:bidscope:separator-password@localhost:5432/bidscope_test"
    ),
    "empty-authority": "postgresql+asyncpg:///bidscope_test",
    "arbitrary-host": (
        "postgresql+asyncpg://bidscope:external-password@external.example:5432/bidscope_test"
    ),
    "invalid-port": (
        "postgresql+asyncpg://bidscope:port-password@localhost:not-a-port/bidscope_test"
    ),
    "empty-database": "postgresql+asyncpg://bidscope:empty-db-password@localhost:5432/",
}

_EARLY_REJECTION_CASES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "database": (
        "postgresql+asyncpg://bidscope:primary-password-injected@localhost:5432/primary-database-name-injected",
        _VALID_CHECKPOINT_URL,
        (
            "primary-password-injected",
            "primary-database-name-injected",
            "valid-checkpoint-password",
        ),
    ),
    "checkpoint": (
        _VALID_DATABASE_URL,
        "postgresql+psycopg://bidscope:checkpoint-password-injected@localhost:5432/checkpoint-database-name-injected",
        (
            "checkpoint-password-injected",
            "checkpoint-database-name-injected",
            "valid-password",
        ),
    ),
}


def _call_guard_with_case(case_id: str) -> None:
    """Invoke the real guard without retaining raw DSNs in this frame."""
    _call_guard_with_urls(_CASES[case_id][0], _CASES[case_id][1])


def _call_guard_with_urls(database_url: str, checkpoint_url: str) -> None:
    """Invoke the real guard without retaining raw DSNs in this frame."""
    from bidscope.config import get_settings

    get_settings.cache_clear()
    try:
        overrides = {
            "BIDSCOPE_APP_MODE": "test",
            "BIDSCOPE_DATABASE_URL": database_url,
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": checkpoint_url,
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            overrides.clear()
            del database_url
            del checkpoint_url
            enforce_test_environment()
    finally:
        get_settings.cache_clear()


def _call_guard_with_malformed_case(case_id: str, field: str) -> None:
    url = _MALFORMED_URLS[case_id]
    try:
        if field == "database":
            _call_guard_with_urls(url, _VALID_CHECKPOINT_URL)
        else:
            _call_guard_with_urls(_VALID_DATABASE_URL, url)
    finally:
        del url


def _call_guard_with_early_case(case_id: str) -> None:
    _call_guard_with_urls(
        _EARLY_REJECTION_CASES[case_id][0], _EARLY_REJECTION_CASES[case_id][1]
    )


def _iter_exception_graph(exception: BaseException) -> Iterator[BaseException]:
    """Yield an exception and all linked exceptions without looping."""
    seen: set[int] = set()
    pending: list[BaseException] = [exception]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, ExceptionGroup):
            pending.extend(current.exceptions)


def _assert_no_raw_secret_leaks(error: BaseException, secrets: tuple[str, ...]) -> None:
    for exception in _iter_exception_graph(error):
        rendered = (
            str(exception),
            repr(exception),
            "".join(traceback.format_exception(exception)),
            repr(exception.args),
            repr(exception.__cause__),
            repr(exception.__context__),
        )
        for value in rendered:
            for secret in secrets:
                assert secret not in value
        frame = exception.__traceback__
        while frame is not None:
            if frame.tb_frame.f_code.co_filename.replace("\\", "/").endswith(
                "bidscope/testing/env_guard.py"
            ):
                assert all(
                    secret not in repr(frame.tb_frame.f_locals) for secret in secrets
                )
            frame = frame.tb_next


@pytest.mark.parametrize("case_id", tuple(_CASES))
def test_mismatched_database_urls_never_expose_passwords(case_id: str) -> None:
    with pytest.raises(_GUARD_FAILURE) as error:
        _call_guard_with_case(case_id)

    secrets = _CASES[case_id][2]
    _assert_no_raw_secret_leaks(error.value, secrets)
    message = str(error.value)
    if case_id in {"malformed-scheme", "encoded-malformed-scheme", "unknown-scheme"}:
        assert "database_url='<redacted PostgreSQL URL>'" in message
        assert "checkpoint_database_url='<redacted PostgreSQL URL>'" in message
    else:
        assert "database_url='postgresql+asyncpg://" in message
        assert "checkpoint_database_url='postgresql+psycopg://" in message
    if case_id in {"normal", "encoded"}:
        assert "bidscope_test" in message


@pytest.mark.parametrize("case_id", tuple(_MALFORMED_URLS))
@pytest.mark.parametrize("field", ("database", "checkpoint"))
def test_malformed_database_urls_are_rejected(case_id: str, field: str) -> None:
    with pytest.raises(_GUARD_FAILURE):
        _call_guard_with_malformed_case(case_id, field)


@pytest.mark.parametrize("case_id", tuple(_EARLY_REJECTION_CASES))
def test_early_database_suffix_failures_are_constant_and_secret_free(case_id: str) -> None:
    with pytest.raises(_GUARD_FAILURE) as error:
        _call_guard_with_early_case(case_id)

    _assert_no_raw_secret_leaks(error.value, _EARLY_REJECTION_CASES[case_id][2])
    assert str(error.value) == (
        "database URL must target a dedicated test database (name must end with "
        "'_test' or '_e2e')"
        if case_id == "database"
        else "checkpoint URL must target a dedicated test database (name must end with "
        "'_test' or '_e2e')"
    )
