"""Regression tests for production database settings secrecy and validation."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator

import pytest
from bidscope.config import Settings
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
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
        (
            "database_url",
            "postgresql+asyncpg://user:password@host.example.test:/bidscope",
        ),
        (
            "database_url",
            "postgresql+asyncpg://user:password@[2001:db8::1]:/bidscope",
        ),
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
        (
            "checkpoint_database_url",
            "postgresql+psycopg://user:password@host.example.test:/bidscope",
        ),
    ),
)
def test_production_database_urls_fail_closed_on_unsafe_structure(
    field: str, value: str
) -> None:
    settings = valid_production_settings()
    settings[field] = value

    error = _settings_validation_error(settings)

    assert error.errors(include_context=True)[0]["input"][field] == "**********"
    assert value not in str(error)
    _assert_exception_graph_is_secret_free(error, "password")


@pytest.mark.parametrize("host", ("database.example.test", "[2001:db8::1]"))
def test_production_allows_an_external_database_authority_without_a_port(host: str) -> None:
    settings_values = valid_production_settings()
    settings_values["database_url"] = (
        "postgresql+asyncpg://bidscope:"
        f"{DSN_PASSWORD}@{host}/bidscope"
    )
    settings_values["checkpoint_database_url"] = (
        "postgresql+psycopg://bidscope:"
        f"{CHECKPOINT_PASSWORD}@{host}/bidscope"
    )

    settings = Settings(**settings_values)

    expected_host = host.removeprefix("[").removesuffix("]")
    assert make_url(settings.database_url.get_secret_value()).host == expected_host
    checkpoint_url = make_url(settings.checkpoint_database_url.get_secret_value())
    assert checkpoint_url.host == expected_host


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("database_url", "postgresql+asyncpg://database.example.test:5432/bidscope"),
        ("database_url", "postgresql+asyncpg://:password@database.example.test:5432/bidscope"),
        ("database_url", "postgresql+asyncpg://user:@database.example.test:5432/bidscope"),
        ("checkpoint_database_url", "postgresql+psycopg://database.example.test:5432/bidscope"),
        (
            "checkpoint_database_url",
            "postgresql+psycopg://:password@database.example.test:5432/bidscope",
        ),
        (
            "checkpoint_database_url",
            "postgresql+psycopg://user:@database.example.test:5432/bidscope",
        ),
    ),
)
def test_production_dsn_requires_explicit_nonempty_username_and_password(
    field: str, value: str
) -> None:
    settings = valid_production_settings()
    settings[field] = value

    error = _settings_validation_error(settings)

    item = error.errors(include_context=True)[0]
    assert item["input"][field] == "**********"
    assert value not in str(error)
    _assert_exception_graph_is_secret_free(error, value)


@pytest.mark.parametrize(
    ("field", "query_key"),
    (
        ("database_url", "ssl"),
        ("checkpoint_database_url", "sslmode"),
    ),
)
def test_production_allows_only_the_supported_tls_query_for_each_driver(
    field: str, query_key: str
) -> None:
    settings_values = valid_production_settings()
    settings_values[field] = f"{settings_values[field]}?{query_key}=require"

    settings = Settings(**settings_values)
    url = make_url(getattr(settings, field).get_secret_value())
    _args, connect_kwargs = create_engine(url).dialect.create_connect_args(url)

    assert connect_kwargs[query_key] == "require"


@pytest.mark.parametrize(
    ("field", "query"),
    (
        ("database_url", "sslmode=require"),
        ("checkpoint_database_url", "ssl=require"),
        ("database_url", "ssl="),
        ("database_url", ""),
        ("checkpoint_database_url", "sslmode=disable"),
        ("database_url", "host=override.example.test"),
        ("database_url", "port=65432"),
        ("database_url", "database=other_database"),
        ("checkpoint_database_url", "dbname=other_database"),
        ("checkpoint_database_url", "service=override-service"),
        ("database_url", "user=override-user"),
        ("checkpoint_database_url", "password=query-secret"),
        ("database_url", "passfile=/tmp/credentials"),
        ("database_url", "unknown=unsafe"),
        ("database_url", "ssl=require&ssl=require"),
    ),
)
def test_production_rejects_target_overrides_unknown_and_unsafe_dsn_queries(
    field: str, query: str
) -> None:
    settings = valid_production_settings()
    value = f"{settings[field]}?{query}"
    settings[field] = value

    error = _settings_validation_error(settings)

    item = error.errors(include_context=True)[0]
    assert item["input"][field] == "**********"
    assert value not in str(error)
    _assert_exception_graph_is_secret_free(error, value, "query-secret")


@pytest.mark.parametrize(
    ("field", "value", "secrets"),
    (
        ("database_url", "postgresql+asyncpg://bare-secret", ("bare-secret",)),
        (
            "database_url",
            "mysql://user:wrong-secret@internal-target.invalid/database-sentinel",
            ("wrong-secret", "internal-target.invalid", "database-sentinel"),
        ),
        (
            "database_url",
            "postgresql+asyncpg://user:password@database.example.test:5432/bidscope"
            "#fragment-secret",
            ("password", "fragment-secret"),
        ),
    ),
)
def test_invalid_production_dsn_errors_always_use_a_fixed_mask(
    field: str, value: str, secrets: tuple[str, ...]
) -> None:
    settings = valid_production_settings()
    settings[field] = value

    error = _settings_validation_error(settings)

    structured = error.errors(include_url=True, include_context=True)
    item = structured[0]
    assert item["input"][field] == "**********"
    context = item.get("ctx", {})
    assert isinstance(context["error"], ValueError)
    assert context["error"].__traceback__ is None
    rendered = (
        str(error),
        repr(error),
        str(structured),
        error.json(),
        "".join(traceback.format_exception(error)),
    )
    for candidate in (*secrets, value):
        assert all(candidate not in rendered_value for rendered_value in rendered)
    _assert_exception_graph_is_secret_free(error, *secrets, value)


def _secret_field_type_error(field: str) -> ValidationError:
    secret = f"{field}-type-secret"
    settings = valid_production_settings()
    settings[field] = {"nested": secret}
    try:
        Settings(**settings)
    except ValidationError as error:
        del settings
        del secret
        return error
    raise AssertionError("Settings should reject a non-string secret field")


@pytest.mark.parametrize(
    "field",
    ("admin_token", "model_api_key", "s3_access_key", "s3_secret_key"),
)
def test_secret_field_type_errors_use_fixed_masks(field: str) -> None:
    secret = f"{field}-type-secret"
    error = _secret_field_type_error(field)

    item = next(item for item in error.errors(include_context=True) if item["loc"] == (field,))
    assert item["input"] == "**********"
    assert secret not in str(error)
    _assert_exception_graph_is_secret_free(error, secret)


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
