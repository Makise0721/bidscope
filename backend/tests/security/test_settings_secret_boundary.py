"""Regression tests for production database settings secrecy and validation."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator

import pytest
from bidscope.config import Settings
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

DSN_PASSWORD = "settings-dsn-password-4a9f"
CHECKPOINT_PASSWORD = "settings-checkpoint-password-8c2d"


def valid_production_settings() -> dict[str, object]:
    return {
        "app_mode": "production",
        "admin_token": "a" * 32,
        "object_store_type": "s3",
        "s3_endpoint": "https://s3.example.test",
        "s3_bucket": "bidscope-prod",
        "s3_access_key": "access-key",
        "s3_secret_key": "secret-key",
        "allowed_origins": ["https://bidscope.example.test"],
        "trusted_hosts": ["bidscope.example.test"],
        "external_scheme": "https",
        "database_url": (
            "postgresql+asyncpg://bidscope:"
            f"{DSN_PASSWORD}@database.example.test:5432/bidscope"
        ),
        "checkpoint_database_url": (
            "postgresql+psycopg://bidscope:"
            f"{CHECKPOINT_PASSWORD}@database.example.test:5432/bidscope"
        ),
    }


def _iter_exception_graph(error: BaseException) -> Iterator[BaseException]:
    pending = [error]
    seen: set[int] = set()
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


def _assert_exception_graph_is_secret_free(error: BaseException, *secrets: str) -> None:
    for exception in _iter_exception_graph(error):
        rendered = (
            str(exception),
            repr(exception),
            repr(exception.args),
            "".join(traceback.format_exception(exception)),
        )
        for value in rendered:
            for secret in secrets:
                assert secret not in value
        frame = exception.__traceback__
        while frame is not None:
            locals_repr = repr(frame.tb_frame.f_locals)
            for secret in secrets:
                assert secret not in locals_repr
            frame = frame.tb_next


def _settings_validation_error(settings: dict[str, object]) -> ValidationError:
    try:
        Settings(**settings)
    except ValidationError as error:
        del settings
        return error
    raise AssertionError("Settings should reject the invalid production configuration")


def _invalid_dsn_validation_error() -> ValidationError:
    settings = valid_production_settings()
    settings["database_url"] = (
        "postgresql+asyncpg://bidscope:"
        f"{DSN_PASSWORD}@database.example.test:5432/bidscope?host=override.example.test"
    )
    return _settings_validation_error(settings)


def test_production_rejects_the_implicit_demo_database_urls() -> None:
    settings = valid_production_settings()
    settings.pop("database_url")
    settings.pop("checkpoint_database_url")

    error = _settings_validation_error(settings)

    rendered = (str(error), str(error.errors()), error.json())
    for value in rendered:
        assert "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope" not in value
        assert "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope" not in value


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("database_url", "postgresql+asyncpg://user:password@host.example.test"),
        ("database_url", "postgresql+asyncpg://user:password@/bidscope"),
        ("database_url", "postgresql+asyncpg://user:password@host.example.test:5432/"),
        (
            "database_url",
            "postgresql+asyncpg://user:password@host.example.test:5432/bidscope?sslmode=require",
        ),
        (
            "database_url",
            "postgresql+asyncpg://user:password@host.example.test:5432/bidscope#fragment",
        ),
        (
            "database_url",
            "postgresql+psycopg://user:password@host.example.test:5432/bidscope",
        ),
        (
            "checkpoint_database_url",
            "postgresql+asyncpg://user:password@host.example.test:5432/bidscope",
        ),
    ),
)
def test_production_database_urls_fail_closed_on_unsafe_structure(
    field: str, value: str
) -> None:
    settings = valid_production_settings()
    settings[field] = value

    error = _settings_validation_error(settings)

    assert field in str(error)
    assert value not in str(error)
    _assert_exception_graph_is_secret_free(error, "password")


def test_production_allows_an_external_database_authority() -> None:
    settings = Settings(**valid_production_settings())

    assert make_url(settings.database_url.get_secret_value()).host == "database.example.test"
    checkpoint_url = make_url(settings.checkpoint_database_url.get_secret_value())
    assert checkpoint_url.host == "database.example.test"


def test_successful_settings_mask_database_urls_in_every_public_serialization() -> None:
    settings = Settings(**valid_production_settings())

    rendered = (
        str(settings),
        repr(settings),
        repr(settings.model_dump()),
        json.dumps(settings.model_dump(mode="json")),
    )
    for value in rendered:
        assert DSN_PASSWORD not in value
        assert CHECKPOINT_PASSWORD not in value

    python_dump = settings.model_dump()
    assert isinstance(python_dump["database_url"], SecretStr)
    assert isinstance(python_dump["checkpoint_database_url"], SecretStr)
    assert isinstance(settings.database_url, SecretStr)
    assert isinstance(settings.checkpoint_database_url, SecretStr)
    assert settings.database_url.get_secret_value().endswith("/bidscope")


def test_production_dsn_validation_context_does_not_retain_the_original_exception() -> None:
    error = _invalid_dsn_validation_error()

    for item in error.errors(include_context=True):
        context = item.get("ctx", {})
        context_error = context.get("error")
        assert isinstance(context_error, ValueError)
        assert context_error.__traceback__ is None
        assert DSN_PASSWORD not in repr(context_error.args)
    _assert_exception_graph_is_secret_free(error, DSN_PASSWORD, CHECKPOINT_PASSWORD)
