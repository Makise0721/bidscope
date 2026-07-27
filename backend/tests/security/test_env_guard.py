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
        "postgresql+psycopg://bidscope:raw-checkpoint-password@otherhost:5432/bidscope_test",
        ("raw-db-password", "raw-checkpoint-password"),
    ),
    "encoded": (
        "postgresql+asyncpg://bidscope:encoded-db-p%40ss%3Aword%2F%3F%23%5B%5D@localhost:5432/bidscope_test",
        "postgresql+psycopg://bidscope:encoded-checkpoint-p%40ss%3Aword%2F%3F%23%5B%5D@otherhost:5432/bidscope_test",
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
}


def _call_guard_with_case(case_id: str) -> None:
    """Invoke the real guard without retaining raw DSNs in this frame."""
    from bidscope.config import get_settings

    get_settings.cache_clear()
    try:
        overrides = {
            "BIDSCOPE_APP_MODE": "test",
            "BIDSCOPE_DATABASE_URL": _CASES[case_id][0],
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": _CASES[case_id][1],
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            overrides.clear()
            enforce_test_environment()
    finally:
        get_settings.cache_clear()


def _iter_exception_graph(exception: BaseException) -> Iterator[BaseException]:
    """Yield an exception and all linked exceptions without looping."""
    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _assert_no_raw_secret_leaks(error: BaseException, secrets: tuple[str, ...]) -> None:
    rendered = (
        str(error),
        repr(error),
        "".join(traceback.format_exception(error)),
        repr(error.__cause__),
        repr(error.__context__),
    )
    for value in rendered:
        for secret in secrets:
            assert secret not in value

    for exception in _iter_exception_graph(error):
        assert all(secret not in repr(exception.args) for secret in secrets)
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
    assert "database_url='postgresql+asyncpg://" in message
    assert "checkpoint_database_url='postgresql+psycopg://" in message
    if case_id in {"normal", "encoded"}:
        assert "bidscope_test" in message
