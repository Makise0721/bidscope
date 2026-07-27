"""Security regressions for test environment database URL failures."""

from __future__ import annotations

import os
import traceback
from collections.abc import Iterator
from unittest import mock

import pytest
from bidscope.testing import enforce_test_environment
from sqlalchemy.engine import make_url

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
    "query-host-override": (
        "postgresql+asyncpg://bidscope:query-host-password@localhost:5432/bidscope_test"
        "?host=external.example"
    ),
    "query-port-override": (
        "postgresql+asyncpg://bidscope:query-port-password@localhost:5432/bidscope_test"
        "?port=65432"
    ),
    "query-dbname-override": (
        "postgresql+asyncpg://bidscope:query-dbname-password@localhost:5432/bidscope_test"
        "?dbname=production_database"
    ),
    "query-service-override": (
        "postgresql+asyncpg://bidscope:query-service-password@localhost:5432/bidscope_test"
        "?service=production-service"
    ),
    "fragment": (
        "postgresql+asyncpg://bidscope:fragment-password@localhost:5432/bidscope_test"
        "#external-target"
    ),
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
    assert str(error.value) == "Integration test database URLs are invalid or mismatched."


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
    assert str(error.value) == "Integration test database URLs are invalid or mismatched."


@pytest.mark.parametrize("case_id", tuple(_MALFORMED_URLS))
def test_malformed_urls_emit_a_fixed_secret_free_guard_failure(case_id: str) -> None:
    raw_url = _MALFORMED_URLS[case_id]
    raw_database_name = "bidscope_test"
    password = raw_url.partition("://")[2].partition("@")[0].partition(":")[2]

    with pytest.raises(_GUARD_FAILURE) as error:
        _call_guard_with_malformed_case(case_id, "database")

    assert str(error.value) == "Integration test database URLs are invalid or mismatched."
    _assert_no_raw_secret_leaks(
        error.value,
        tuple(value for value in (password, raw_database_name, "production_database") if value),
    )


@pytest.mark.parametrize(
    "case_id",
    (
        "query-host-override",
        "query-port-override",
        "query-dbname-override",
        "query-service-override",
        "fragment",
    ),
)
def test_url_parser_preserves_rejected_query_or_fragment_components(case_id: str) -> None:
    parsed = make_url(_MALFORMED_URLS[case_id])

    if case_id == "fragment":
        assert "#external-target" in _MALFORMED_URLS[case_id]
    else:
        assert parsed.query
