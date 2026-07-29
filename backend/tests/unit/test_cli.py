"""CLI runtime compatibility tests."""

from __future__ import annotations

import json
import os
import re
import traceback
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock

import pytest
from bidscope import cli
from bidscope.config import Settings
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

PRODUCTION_SECRET_VALUES = {
    "admin_token": "arbitrary-admin-secret-7f2a-123456",
    "s3_access_key": "arbitrary-access-secret-8b3c",
    "s3_secret_key": "arbitrary-s3-secret-9d4e",
    "model_api_key": "arbitrary-model-secret-1e5f",
}

TRACEBACK_DIRECT_ADMIN_SECRET = "traceback-direct-admin-secret-7f2a"
TRACEBACK_DIRECT_ACCESS_SECRET = "traceback-direct-access-secret-8b3c"
TRACEBACK_DIRECT_S3_SECRET = "traceback-direct-s3-secret-9d4e"
TRACEBACK_DIRECT_MODEL_SECRET = "traceback-direct-model-secret-1e5f"
TRACEBACK_TYPED_MODEL_SECRET = "traceback-typed-model-secret-2a6b"
TRACEBACK_DSN_PASSWORD = "traceback-dsn-p@ss:word/?#[]"
TRACEBACK_DSN_PASSWORD_ENCODED = "traceback-dsn-p%40ss%3Aword%2F%3F%23%5B%5D"
TRACEBACK_ENV_MODEL_SECRET = "traceback-environment-model-secret-9d4e"
TRACEBACK_ENV_DSN_PASSWORD = "traceback-environment-dsn-p@ss:word/?#[]"
TRACEBACK_ENV_DSN_PASSWORD_ENCODED = (
    "traceback-environment-dsn-p%40ss%3Aword%2F%3F%23%5B%5D"
)
TRACEBACK_DIRECT_SECRET_VALUES = {
    "admin_token": TRACEBACK_DIRECT_ADMIN_SECRET,
    "model_api_key": TRACEBACK_DIRECT_MODEL_SECRET,
    "s3_access_key": TRACEBACK_DIRECT_ACCESS_SECRET,
    "s3_secret_key": TRACEBACK_DIRECT_S3_SECRET,
}


def valid_production_settings() -> dict[str, object]:
    return {
        "app_mode": "production",
        "admin_token": "a" * 32,
        "object_store_type": "s3",
        "s3_endpoint": "https://s3.example.test",
        "s3_bucket": "bidscope-prod",
        "s3_access_key": "test-access-key",
        "s3_secret_key": "test-secret-key",
        "allowed_origins": ["https://bidscope.example.test"],
        "trusted_hosts": ["bidscope.example.test"],
        "external_scheme": "https",
        "database_url": "postgresql+asyncpg://bidscope:test-password@database.example.test:5432/bidscope",
        "checkpoint_database_url": "postgresql+psycopg://bidscope:test-checkpoint-password@database.example.test:5432/bidscope",
    }


@pytest.mark.parametrize(
    ("platform", "expected_calls"),
    [("win32", 1), ("linux", 0)],
)
def test_configure_windows_selector_event_loop_policy_only_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected_calls: int,
) -> None:
    """The API launcher installs psycopg's required policy only on Windows."""
    policy = object()
    policy_factory = Mock(return_value=policy)
    set_policy = Mock()
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(
        cli.asyncio,
        "WindowsSelectorEventLoopPolicy",
        policy_factory,
        raising=False,
    )
    monkeypatch.setattr(cli.asyncio, "set_event_loop_policy", set_policy)

    cli.configure_windows_selector_event_loop_policy()

    assert policy_factory.call_count == expected_calls
    if expected_calls:
        set_policy.assert_called_once_with(policy)
    else:
        set_policy.assert_not_called()


def test_settings_requires_a_positive_stale_run_threshold() -> None:
    """Startup recovery cannot accept a zero or negative stale-age threshold."""
    assert Settings().stale_run_after_seconds == 300
    with pytest.raises(ValidationError):
        Settings(stale_run_after_seconds=0)


def test_heartbeat_interval_is_positive_and_shorter_than_stale_threshold() -> None:
    """Execution heartbeats must run often enough to protect a live claim."""
    settings = Settings(run_heartbeat_seconds=30, stale_run_after_seconds=60)
    assert settings.run_heartbeat_seconds == 30
    with pytest.raises(ValidationError):
        Settings(run_heartbeat_seconds=0)
    with pytest.raises(ValidationError):
        Settings(run_heartbeat_seconds=60, stale_run_after_seconds=60)


def test_production_settings_require_an_admin_token() -> None:
    settings = valid_production_settings()
    settings.pop("admin_token")

    with pytest.raises(ValidationError, match="admin_token"):
        Settings(**settings)


def test_backup_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.backup_root
    assert settings.backup_daily_retention == 7
    assert settings.backup_weekly_retention == 4
    assert settings.backup_s3_enabled is False
    assert settings.backup_s3_prefix == "bidscope-backups"
    assert settings.backup_tool_timeout_seconds == 900


def test_backup_retention_and_tool_timeout_must_be_positive() -> None:
    for field_name in (
        "backup_daily_retention",
        "backup_weekly_retention",
        "backup_tool_timeout_seconds",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field_name: 0})


def test_backup_external_s3_requires_all_explicit_fields() -> None:
    with pytest.raises(ValidationError, match="backup_s3_endpoint"):
        Settings(_env_file=None, backup_s3_enabled=True)


def test_backup_external_s3_accepts_complete_explicit_configuration() -> None:
    settings = Settings(
        _env_file=None,
        backup_s3_enabled=True,
        backup_s3_endpoint="https://backup-s3.example.test",
        backup_s3_bucket="bidscope-backups",
        backup_s3_access_key="backup-access",
        backup_s3_secret_key="backup-secret",
    )

    assert settings.backup_s3_enabled is True


def test_settings_default_to_safe_nonproduction_configuration() -> None:
    settings = Settings()

    assert settings.admin_token_min_length == 32
    assert settings.allowed_origins == []
    assert settings.trusted_hosts == []
    assert settings.external_scheme == "http"
    assert settings.s3_region == "us-east-1"


def test_settings_default_to_bounded_runtime_limits() -> None:
    settings = Settings()

    assert settings.db_pool_size > 0
    assert settings.db_max_overflow > 0
    assert settings.db_pool_recycle_seconds > 0
    assert settings.db_connect_timeout_seconds > 0
    assert settings.db_command_timeout_seconds > 0
    assert settings.s3_connect_timeout_seconds > 0
    assert settings.s3_read_timeout_seconds > 0
    assert settings.s3_max_attempts > 0
    assert settings.max_concurrent_runs > 0
    assert settings.max_request_body_bytes > 0
    assert settings.max_sse_connections > 0
    assert settings.max_report_items > 0
    assert settings.graceful_shutdown_seconds > settings.scheduler_tick_timeout_seconds


@pytest.mark.parametrize(
    "field_name",
    (
        "db_pool_size",
        "db_max_overflow",
        "db_pool_recycle_seconds",
        "db_connect_timeout_seconds",
        "db_command_timeout_seconds",
        "s3_connect_timeout_seconds",
        "s3_read_timeout_seconds",
        "s3_max_attempts",
        "max_concurrent_runs",
        "max_request_body_bytes",
        "max_sse_connections",
        "max_report_items",
        "graceful_shutdown_seconds",
        "scheduler_tick_timeout_seconds",
    ),
)
def test_runtime_limits_must_be_positive(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        Settings(**{field_name: 0})


def test_graceful_shutdown_must_cover_scheduler_tick_timeout() -> None:
    with pytest.raises(ValidationError, match="scheduler_tick_timeout_seconds"):
        Settings(graceful_shutdown_seconds=10, scheduler_tick_timeout_seconds=10)
    with pytest.raises(ValidationError, match="scheduler_tick_timeout_seconds"):
        Settings(graceful_shutdown_seconds=10, scheduler_tick_timeout_seconds=11)


def test_test_mode_keeps_safe_local_defaults() -> None:
    settings = Settings(app_mode="test")

    assert settings.admin_token is None
    assert settings.object_store_type == "local"
    assert settings.external_scheme == "http"
    assert settings.allowed_origins == []
    assert settings.trusted_hosts == []


def test_admin_token_min_length_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="admin_token_min_length"):
        Settings(admin_token_min_length=0)


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize("admin_token", (None, " "))
def test_strict_settings_reject_missing_or_blank_admin_tokens(
    app_mode: str, admin_token: str | None
) -> None:
    settings = valid_production_settings()
    settings.update(app_mode=app_mode, admin_token=admin_token)

    with pytest.raises(ValidationError, match="admin_token"):
        Settings(**settings)


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize("admin_token", ("a" * 4097, " " * 4097))
def test_strict_settings_reject_overlong_ascii_or_whitespace_admin_tokens(
    app_mode: str, admin_token: str
) -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        Settings(app_mode=app_mode, admin_token=admin_token)


@pytest.mark.parametrize("app_mode", ("development", "production"))
@pytest.mark.parametrize(
    ("admin_token", "is_valid"),
    (
        ("a" * 4096, True),
        ("a" * 4097, False),
        ("é" * 2048, True),
        ("é" * 2049, False),
    ),
)
def test_strict_settings_bound_admin_token_by_utf8_byte_length(
    app_mode: str, admin_token: str, is_valid: bool
) -> None:
    settings = valid_production_settings()
    settings.update(app_mode=app_mode, admin_token=admin_token)

    if is_valid:
        assert Settings(**settings).admin_token is not None
    else:
        with pytest.raises(ValidationError, match="admin_token"):
            Settings(**settings)


@pytest.mark.parametrize("app_mode", ("demo", "test"))
@pytest.mark.parametrize("admin_token", ("", " ", "a" * 4097, " " * 4097, "é" * 2049))
def test_bypass_modes_allow_blank_and_overlong_admin_tokens(
    app_mode: str, admin_token: str
) -> None:

    settings = Settings(app_mode=app_mode, admin_token=admin_token)

    assert settings.admin_token is not None
    assert settings.admin_token.get_secret_value() == admin_token


def test_strict_settings_reject_unencodable_admin_tokens() -> None:
    with pytest.raises(ValidationError, match="admin_token"):
        Settings(app_mode="development", admin_token="\ud800")


def assert_validation_error_hides_production_secrets(
    settings: dict[str, object], *additional_secrets: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    raw_secrets = (*PRODUCTION_SECRET_VALUES.values(), *additional_secrets)
    _assert_no_structured_secret_leaks(
        error.value.errors(include_url=True, include_context=True), raw_secrets
    )
    rendered_error = str(error.value)
    structured_error = str(error.value.errors())
    structured_json = error.value.json()
    errors_json = json.dumps(error.value.errors(), default=str)
    for secret in raw_secrets:
        assert secret not in rendered_error
        assert secret not in structured_error
        assert secret not in structured_json
        assert secret not in errors_json


def _iter_exception_graph(error: BaseException):
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)


def _contains_raw_secret(
    value: object, secrets: tuple[str, ...], seen: set[int] | None = None
) -> bool:
    seen = set() if seen is None else seen
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, SecretStr):
        return any(secret in value.get_secret_value() for secret in secrets)
    if isinstance(value, BaseException):
        return _contains_raw_secret(value.args, secrets, seen)
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return False
        seen.add(value_id)
        return any(
            _contains_raw_secret(item, secrets, seen)
            for pair in tuple(value.items())
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in seen:
            return False
        seen.add(value_id)
        return any(_contains_raw_secret(item, secrets, seen) for item in tuple(value))
    value_id = id(value)
    if value_id in seen:
        return False
    seen.add(value_id)
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        try:
            if _contains_raw_secret(getter(), secrets, seen):
                return True
        except Exception:
            pass
    try:
        attributes = vars(value)
    except TypeError:
        attributes = {}
    return _contains_raw_secret(attributes, secrets, seen)


def _assert_no_structured_secret_leaks(
    value: object,
    secrets: tuple[str, ...],
    seen: set[int] | None = None,
) -> None:
    seen = set() if seen is None else seen
    getter = getattr(value, "get_secret_value", None)
    assert getter is None, f"recoverable secret object survived: {value!r}"
    assert not isinstance(value, SecretStr)
    if isinstance(value, str):
        for secret in secrets:
            assert secret not in value
        return
    if isinstance(value, BaseException):
        _assert_no_structured_secret_leaks(value.args, secrets, seen)
        return
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)
        for key, item in value.items():
            _assert_no_structured_secret_leaks(key, secrets, seen)
            _assert_no_structured_secret_leaks(item, secrets, seen)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)
        for item in value:
            _assert_no_structured_secret_leaks(item, secrets, seen)


def assert_validation_error_has_no_traceback_secret_leaks(
    error: ValidationError, *secrets: str
) -> None:
    raw_secrets = tuple(secrets)
    _assert_no_structured_secret_leaks(
        error.errors(include_url=True, include_context=True), raw_secrets
    )
    rendered = (
        str(error),
        str(error.errors()),
        error.json(),
        "".join(traceback.format_exception(error)),
    )
    for value in rendered:
        for secret in raw_secrets:
            assert secret not in value

    for exception in _iter_exception_graph(error):
        assert not _contains_raw_secret(exception.args, raw_secrets)
        traceback_frame = exception.__traceback__
        while traceback_frame is not None:
            assert not _contains_raw_secret(
                traceback_frame.tb_frame.f_locals, raw_secrets
            )
            traceback_frame = traceback_frame.tb_next

    assert error.__cause__ is None
    assert error.__context__ is None


def test_direct_raw_model_key_has_no_traceback_secret_locals() -> None:
    with pytest.raises(ValidationError, match="external_scheme") as error:
        Settings(
            **{
                **valid_production_settings(),
                "real_model_enabled": True,
                "model_api_key": TRACEBACK_DIRECT_MODEL_SECRET,
                "database_url": (
                    "postgresql+asyncpg://bidscope:"
                    f"{TRACEBACK_DSN_PASSWORD_ENCODED}@database.example.test:5432/bidscope"
                ),
                "external_scheme": "http",
            }
        )

    assert_validation_error_has_no_traceback_secret_leaks(
        error.value,
        TRACEBACK_DIRECT_MODEL_SECRET,
        TRACEBACK_DSN_PASSWORD,
        TRACEBACK_DSN_PASSWORD_ENCODED,
    )


def test_direct_secretstr_model_key_has_no_traceback_secret_locals() -> None:
    with pytest.raises(ValidationError, match="external_scheme") as error:
        Settings(
            **{
                **valid_production_settings(),
                "real_model_enabled": True,
                "model_api_key": SecretStr(TRACEBACK_TYPED_MODEL_SECRET),
                "checkpoint_database_url": (
                    "postgresql+psycopg://bidscope:"
                    f"{TRACEBACK_DSN_PASSWORD_ENCODED}@database.example.test:5432/bidscope"
                ),
                "external_scheme": "http",
            }
        )

    assert_validation_error_has_no_traceback_secret_leaks(
        error.value,
        TRACEBACK_TYPED_MODEL_SECRET,
        TRACEBACK_DSN_PASSWORD,
        TRACEBACK_DSN_PASSWORD_ENCODED,
    )


def _construct_settings_with_direct_secret(
    field_name: str, typed: bool
) -> ValidationError:
    settings = valid_production_settings()
    secret = TRACEBACK_DIRECT_SECRET_VALUES[field_name]
    settings[field_name] = SecretStr(secret) if typed else secret
    if field_name == "model_api_key":
        settings["real_model_enabled"] = True
    settings.update(
        {
            "database_url": (
                "postgresql+asyncpg://bidscope:"
                f"{TRACEBACK_DSN_PASSWORD_ENCODED}@database.example.test:5432/bidscope"
            ),
            "checkpoint_database_url": (
                "postgresql+psycopg://bidscope:"
                f"{TRACEBACK_DSN_PASSWORD_ENCODED}@database.example.test:5432/bidscope"
            ),
            "external_scheme": "http",
        }
    )
    try:
        Settings(**settings)
    except ValidationError as error:
        del settings
        del secret
        return error
    raise AssertionError("Settings should reject the invalid production configuration")


@pytest.mark.parametrize(
    ("field_name", "typed"),
    (
        ("admin_token", False),
        ("admin_token", True),
        ("model_api_key", False),
        ("model_api_key", True),
        ("s3_access_key", False),
        ("s3_access_key", True),
        ("s3_secret_key", False),
        ("s3_secret_key", True),
    ),
)
def test_all_direct_secret_inputs_leave_no_recoverable_traceback_locals(
    field_name: str, typed: bool
) -> None:
    error = _construct_settings_with_direct_secret(field_name, typed)

    assert_validation_error_has_no_traceback_secret_leaks(
        error,
        TRACEBACK_DIRECT_SECRET_VALUES[field_name],
        TRACEBACK_DSN_PASSWORD,
        TRACEBACK_DSN_PASSWORD_ENCODED,
    )


def _set_traceback_secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("BIDSCOPE_"):
            monkeypatch.delenv(key, raising=False)
    environment_values = {
        "BIDSCOPE_APP_MODE": "production",
        "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
        "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
        "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
        "BIDSCOPE_S3_BUCKET": "bidscope-prod",
        "BIDSCOPE_S3_ACCESS_KEY": "traceback-environment-access-secret",
        "BIDSCOPE_S3_SECRET_KEY": "traceback-environment-s3-secret",
        "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
        "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
        "BIDSCOPE_EXTERNAL_SCHEME": "http",
        "BIDSCOPE_REAL_MODEL_ENABLED": "true",
        "BIDSCOPE_MODEL_API_KEY": TRACEBACK_ENV_MODEL_SECRET,
        "BIDSCOPE_DATABASE_URL": (
            "postgresql+asyncpg://bidscope:"
            f"{TRACEBACK_ENV_DSN_PASSWORD_ENCODED}@database.example.test:5432/bidscope"
        ),
    }
    for key, value in environment_values.items():
        monkeypatch.setenv(key, value)


def test_environment_loaded_secrets_have_no_traceback_secret_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_traceback_secret_environment(monkeypatch)

    with pytest.raises(ValidationError, match="external_scheme") as error:
        Settings(_env_file=None)

    error_value = error.value
    del error
    del monkeypatch
    assert_validation_error_has_no_traceback_secret_leaks(
        error_value,
        TRACEBACK_ENV_MODEL_SECRET,
        TRACEBACK_ENV_DSN_PASSWORD,
        TRACEBACK_ENV_DSN_PASSWORD_ENCODED,
        "traceback-environment-access-secret",
        "traceback-environment-s3-secret",
    )



@pytest.mark.parametrize(
    ("field_name", "driver"),
    (
        ("database_url", "asyncpg"),
        ("checkpoint_database_url", "psycopg"),
    ),
)
def test_direct_settings_validation_redacts_dsn_passwords(
    field_name: str,
    driver: str,
) -> None:
    password = "direct-dsn-p@ss:word/?#[]"
    encoded_password = "direct-dsn-p%40ss%3Aword%2F%3F%23%5B%5D"
    dsn = (
        f"postgresql+{driver}://bidscope:{encoded_password}"
        "@database.example.test:5432/bidscope"
    )
    settings = valid_production_settings()
    settings.update({field_name: dsn, "external_scheme": "http"})

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    rendered = (str(error.value), str(error.value.errors()), error.value.json())
    for value in rendered:
        assert password not in value
        assert encoded_password not in value

    sanitized_item = error.value.errors()[0]
    assert sanitized_item["input"][field_name] == "**********"
    assert sanitized_item["type"] == "value_error"
    assert sanitized_item["loc"] == ()
    assert sanitized_item["msg"] == (
        "Value error, production requires valid values for: external_scheme"
    )
    assert sanitized_item["url"].endswith("/value_error")


def test_direct_settings_validation_redacts_malformed_raw_dsn_password() -> None:
    password = "malformed-raw-p@ss:word/?#[]"
    settings = valid_production_settings()
    settings.update(
        {
            "database_url": (
                "postgresql+asyncpg://bidscope:"
                f"{password}@database.example.test:5432/bidscope"
            ),
            "external_scheme": "http",
        }
    )

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    for rendered in (str(error.value), str(error.value.errors()), error.value.json()):
        assert password not in rendered
    assert error.value.errors()[0]["input"]["database_url"] == "**********"


@pytest.mark.parametrize(
    ("password", "dsn_suffix"),
    (
        ("malformed-no-host-raw:p/word?#[]", "malformed-no-host-raw:p/word?#[]"),
        (
            "malformed-no-host-encoded:p@ss/word?#[]",
            "malformed-no-host-encoded%3Ap%40ss%2Fword%3F%23%5B%5D",
        ),
    ),
)
def test_direct_settings_validation_redacts_malformed_no_host_dsn_password(
    password: str,
    dsn_suffix: str,
) -> None:
    settings = valid_production_settings()
    settings.update(
        {
            "database_url": f"postgresql+asyncpg://bidscope:{dsn_suffix}",
            "external_scheme": "http",
        }
    )

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    rendered = (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
        "".join(traceback.format_exception(error.value)),
        repr(error.value.__cause__),
        repr(error.value.__context__),
    )
    for value in rendered:
        assert password not in value
        assert dsn_suffix not in value
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert error.value.errors()[0]["input"]["database_url"] == "**********"


def test_sanitized_settings_validation_error_has_no_raw_exception_chain() -> None:
    password = "chained-dsn-p@ss:word/?#[]"
    encoded_password = "chained-dsn-p%40ss%3Aword%2F%3F%23%5B%5D"
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings.update(
        {
            "database_url": (
                "postgresql+asyncpg://bidscope:"
                f"{encoded_password}@database.example.test:5432/bidscope"
            ),
            "external_scheme": "http",
        }
    )

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    sanitized_item = error.value.errors()[0]
    assert sanitized_item["type"] == "value_error"
    assert sanitized_item["loc"] == ()
    assert sanitized_item["msg"] == (
        "Value error, production requires valid values for: external_scheme"
    )
    assert sanitized_item["url"].endswith("/value_error")
    assert error.value.__cause__ is None
    assert error.value.__context__ is None

    rendered = (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
        "".join(traceback.format_exception(error.value)),
    )
    for secret in (*PRODUCTION_SECRET_VALUES.values(), password, encoded_password):
        for value in rendered:
            assert secret not in value


def test_environment_settings_validation_redacts_dsn_passwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in tuple(os.environ):
        if key.startswith("BIDSCOPE_"):
            monkeypatch.delenv(key, raising=False)

    password = "environment-dsn-p@ss:word/?#[]"
    encoded_password = "environment-dsn-p%40ss%3Aword%2F%3F%23%5B%5D"
    values = {
        "BIDSCOPE_APP_MODE": "production",
        "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
        "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
        "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
        "BIDSCOPE_S3_BUCKET": "bidscope-prod",
        "BIDSCOPE_S3_ACCESS_KEY": "test-access-key",
        "BIDSCOPE_S3_SECRET_KEY": "test-secret-key",
        "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
        "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
        "BIDSCOPE_EXTERNAL_SCHEME": "http",
        "BIDSCOPE_DATABASE_URL": (
            "postgresql+asyncpg://bidscope:"
            f"{encoded_password}@database.example.test:5432/bidscope"
        ),
        "BIDSCOPE_CHECKPOINT_DATABASE_URL": (
            "postgresql+psycopg://bidscope:"
            f"{encoded_password}@database.example.test:5432/bidscope"
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    for rendered in (str(error.value), str(error.value.errors()), error.value.json()):
        assert password not in rendered
        assert encoded_password not in rendered

    sanitized_input = error.value.errors()[0]["input"]
    assert sanitized_input["database_url"] == "**********"
    assert sanitized_input["checkpoint_database_url"] == "**********"


def test_environment_loaded_production_secrets_stay_masked_structurally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "BIDSCOPE_APP_MODE": "production",
        "BIDSCOPE_ADMIN_TOKEN": "env-admin-secret-123456789012345678901234",
        "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
        "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
        "BIDSCOPE_S3_BUCKET": "bidscope-prod",
        "BIDSCOPE_S3_ACCESS_KEY": "env-access-secret",
        "BIDSCOPE_S3_SECRET_KEY": "env-s3-secret",
        "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
        "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
        "BIDSCOPE_EXTERNAL_SCHEME": "env-admin-secret-123456789012345678901234",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    for secret in values.values():
        if "secret" in secret or secret.startswith("env-admin"):
            assert secret not in str(error.value)
            assert secret not in str(error.value.errors())
            assert secret not in error.value.json()


def test_environment_loaded_admin_token_honors_string_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in tuple(os.environ):
        if key.startswith("BIDSCOPE_"):
            monkeypatch.delenv(key, raising=False)
    values = {
        "BIDSCOPE_APP_MODE": "production",
        "BIDSCOPE_ADMIN_TOKEN": "a",
        "BIDSCOPE_ADMIN_TOKEN_MIN_LENGTH": "32",
        "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
        "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
        "BIDSCOPE_S3_REGION": "us-east-1",
        "BIDSCOPE_S3_BUCKET": "bidscope-prod",
        "BIDSCOPE_S3_ACCESS_KEY": "env-access-secret",
        "BIDSCOPE_S3_SECRET_KEY": "env-s3-secret",
        "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
        "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
        "BIDSCOPE_EXTERNAL_SCHEME": "https",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match="admin_token_min_length") as error:
        Settings(_env_file=None)

    assert "env-access-secret" not in str(error.value)
    assert "env-s3-secret" not in str(error.value)


def test_typed_secretstr_admin_token_is_accepted() -> None:
    """Direct SecretStr input is transiently validated and remains masked in Settings."""
    settings = valid_production_settings()
    settings.update(
        {
            "admin_token": SecretStr("a" * 32),
            "real_model_enabled": True,
            "model_api_key": SecretStr("typed-model-secret"),
        }
    )

    result = Settings(**settings)

    assert result.admin_token.get_secret_value() == "a" * 32


@pytest.mark.parametrize("secret_field", ("s3_access_key", "s3_secret_key"))
def test_typed_whitespace_s3_secret_is_rejected(secret_field: str) -> None:
    """Direct blank SecretStr S3 credentials fail the configuration boundary."""
    settings = valid_production_settings()
    settings[secret_field] = SecretStr("   ")

    with pytest.raises(ValidationError, match=secret_field) as error:
        Settings(**settings)

    for rendered in (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
    ):
        assert "SecretStr('   ')" not in rendered


@pytest.mark.parametrize("minimum", ["not-an-integer", 0, 32])
def test_admin_token_minimum_edge_is_bounded_and_secret_safe(minimum: object) -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["admin_token"] = "minimum-edge-admin-secret"
    settings["admin_token_min_length"] = minimum

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    rendered = " ".join(
        (str(error.value), str(error.value.errors()), error.value.json())
    )
    for secret in PRODUCTION_SECRET_VALUES.values():
        assert secret not in rendered
    assert "minimum-edge-admin-secret" not in rendered


def test_production_settings_missing_admin_token_hides_other_secrets() -> None:
    settings = valid_production_settings()
    settings.pop("admin_token")
    settings.update(
        {
            key: value
            for key, value in PRODUCTION_SECRET_VALUES.items()
            if key != "admin_token"
        }
    )

    assert_validation_error_hides_production_secrets(settings)


def test_production_settings_reject_placeholder_admin_token_without_leaking_it() -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["admin_token"] = "change-me"

    with pytest.raises(ValidationError, match="placeholder") as error:
        Settings(**settings)

    assert "change-me" not in str(error.value)
    for secret in PRODUCTION_SECRET_VALUES.values():
        assert secret not in str(error.value)


def test_production_settings_reject_too_short_admin_token_without_leaking_secrets() -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["admin_token"] = "s" * 31

    with pytest.raises(ValidationError, match="admin_token_min_length"):
        Settings(**settings)

    assert_validation_error_hides_production_secrets(settings, settings["admin_token"])


def test_production_validation_errors_hide_arbitrary_secrets() -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["external_scheme"] = "http"

    assert_validation_error_hides_production_secrets(settings)


def test_sanitized_errors_replace_nested_secret_objects_with_plain_masks() -> None:
    settings = valid_production_settings()
    typed_secrets = {
        field_name: SecretStr(f"structured-{field_name}-secret")
        for field_name in PRODUCTION_SECRET_VALUES
    }
    settings.update(typed_secrets, real_model_enabled=True, external_scheme="http")

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    structured_errors = error.value.errors(include_url=True, include_context=True)
    structured_secrets = tuple(
        secret.get_secret_value() for secret in typed_secrets.values()
    )
    _assert_no_structured_secret_leaks(structured_errors, structured_secrets)

    sanitized_input = structured_errors[0]["input"]
    assert isinstance(sanitized_input, dict)
    assert sanitized_input["admin_token"] == "**********"
    assert sanitized_input["model_api_key"] == "**********"
    assert sanitized_input["s3_access_key"] == "**********"
    assert sanitized_input["s3_secret_key"] == "**********"
    assert sanitized_input["app_mode"] == "production"
    assert structured_errors[0]["type"] == "value_error"
    assert structured_errors[0]["loc"] == ()
    assert structured_errors[0]["msg"].startswith("Value error,")
    assert structured_errors[0]["url"].endswith("/value_error")


@pytest.mark.parametrize(
    "secret_field",
    ("admin_token", "s3_access_key", "s3_secret_key", "model_api_key"),
)
def test_one_character_secret_validation_errors_keep_structured_metadata(
    secret_field: str,
) -> None:
    settings = valid_production_settings()
    settings[secret_field] = "a"
    if secret_field == "admin_token":
        settings["admin_token_min_length"] = 32
    elif secret_field in {"s3_access_key", "s3_secret_key"}:
        settings["s3_bucket"] = "   "
    else:
        settings["external_scheme"] = "http"

    with pytest.raises(ValidationError) as error:
        Settings(**settings)

    sanitized_item = error.value.errors()[0]
    for rendered in (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
    ):
        assert "'a'" not in rendered
        assert '\"a\"' not in rendered
    assert sanitized_item["type"] == "value_error"
    assert sanitized_item["loc"] == ()
    assert sanitized_item["msg"].startswith("Value error,")
    assert sanitized_item["url"].endswith("/value_error")


def test_secret_unwrapping_does_not_use_private_secret_internals() -> None:
    config_source = Path(__file__).parents[2].joinpath("src/bidscope/config.py").read_text(
        encoding="utf-8"
    )

    assert not re.search(r"\.(?:_secret_value|__dict__)\b", config_source)


def test_field_validation_errors_hide_arbitrary_secrets() -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["external_scheme"] = PRODUCTION_SECRET_VALUES["admin_token"]

    assert_validation_error_hides_production_secrets(settings)


def test_production_template_keeps_database_credentials_blank() -> None:
    template = Path(__file__).parents[3].joinpath(".env.production.example").read_text()

    assert "BIDSCOPE_DATABASE_URL=\n" in template
    assert "BIDSCOPE_CHECKPOINT_DATABASE_URL=\n" in template
    assert "BIDSCOPE_POSTGRES_DB=\n" in template
    assert "BIDSCOPE_POSTGRES_USER=\n" in template
    assert "BIDSCOPE_POSTGRES_PASSWORD=\n" in template
    assert "BIDSCOPE_MODEL_API_KEY=\n" in template
    assert "<database-password>" not in template


def test_production_template_has_active_valid_origin_and_host_entries() -> None:
    template = Path(__file__).parents[3].joinpath(".env.production.example").read_text()

    active_lines = [line for line in template.splitlines() if not line.lstrip().startswith("#")]

    assert 'BIDSCOPE_ALLOWED_ORIGINS=["https://bidscope.example.test"]' in active_lines
    assert 'BIDSCOPE_TRUSTED_HOSTS=["bidscope.example.test"]' in active_lines


def test_production_settings_reject_too_short_admin_token() -> None:
    settings = valid_production_settings()
    settings["admin_token"] = "s" * 31

    with pytest.raises(ValidationError, match="admin_token_min_length") as error:
        Settings(**settings)

    assert settings["admin_token"] not in str(error.value)


def test_production_settings_require_s3_object_store() -> None:
    settings = valid_production_settings()
    settings["object_store_type"] = "local"

    with pytest.raises(ValidationError, match="object_store_type"):
        Settings(**settings)


def test_s3_storage_rejects_whitespace_only_required_fields() -> None:
    for field in (
        "s3_endpoint",
        "s3_region",
        "s3_bucket",
        "s3_access_key",
        "s3_secret_key",
    ):
        settings = valid_production_settings()
        settings[field] = "   "

        with pytest.raises(ValidationError, match=field):
            Settings(**settings)


def test_s3_validation_errors_hide_arbitrary_secret_values_structurally() -> None:
    settings = valid_production_settings()
    settings.update(PRODUCTION_SECRET_VALUES)
    settings["s3_bucket"] = "   "

    assert_validation_error_hides_production_secrets(settings)


def test_real_model_requires_model_api_key_without_leaking_secrets() -> None:
    settings = valid_production_settings()
    settings.update(
        real_model_enabled=True,
        **{
            key: value
            for key, value in PRODUCTION_SECRET_VALUES.items()
            if key != "model_api_key"
        },
    )

    with pytest.raises(ValidationError, match="model_api_key") as error:
        Settings(**settings)

    rendered = (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
        "".join(traceback.format_exception(error.value)),
        repr(error.value.__cause__),
        repr(error.value.__context__),
    )
    for value in rendered:
        for secret in PRODUCTION_SECRET_VALUES.values():
            assert secret not in value


@pytest.mark.parametrize("model_api_key", ("   ", SecretStr("   ")))
def test_real_model_rejects_whitespace_model_api_key(model_api_key: object) -> None:
    settings = valid_production_settings()
    settings.update(real_model_enabled=True, model_api_key=model_api_key)

    with pytest.raises(ValidationError, match="model_api_key") as error:
        Settings(**settings)

    rendered = (
        str(error.value),
        str(error.value.errors()),
        error.value.json(),
        "".join(traceback.format_exception(error.value)),
    )
    assert "SecretStr('   ')" not in " ".join(rendered)
    assert "   " not in error.value.json()


def test_real_model_accepts_and_masks_model_api_key() -> None:
    model_key = PRODUCTION_SECRET_VALUES["model_api_key"]
    settings = Settings(
        **valid_production_settings(),
        real_model_enabled=True,
        model_api_key=SecretStr(model_key),
    )

    assert isinstance(settings.model_api_key, SecretStr)
    assert settings.model_api_key.get_secret_value() == model_key
    assert str(settings.model_api_key) == "**********"
    assert model_key not in str(settings)


def test_real_model_disabled_allows_missing_model_api_key() -> None:
    settings = Settings(**valid_production_settings(), real_model_enabled=False)

    assert settings.model_api_key is None


def test_environment_loaded_real_model_requires_model_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in tuple(os.environ):
        if key.startswith("BIDSCOPE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BIDSCOPE_REAL_MODEL_ENABLED", "true")

    with pytest.raises(ValidationError, match="model_api_key") as error:
        Settings(_env_file=None)

    assert "BIDSCOPE_REAL_MODEL_ENABLED" not in str(error.value)


def test_backup_command_help_exposes_task_5_commands() -> None:
    result = CliRunner().invoke(cli.app, ["ops", "backup", "--help"])

    assert result.exit_code == 0
    for command in ("create", "verify", "list", "prune", "restore"):
        assert command in result.stdout


def test_backup_restore_requires_confirm_without_calling_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restore = Mock()
    monkeypatch.setattr(cli, "_build_backup_service", Mock())
    monkeypatch.setattr(cli.BackupService, "restore", restore)
    (tmp_path / "backup").mkdir()

    result = CliRunner().invoke(
        cli.app,
        [
            "ops",
            "backup",
            "restore",
            str(tmp_path / "backup"),
            "--target-database-url",
            "postgresql://user:password@localhost/app",
            "--target-checkpoint-database-url",
            "postgresql://user:password@localhost/app",
            "--target-object-root",
            str(tmp_path / "objects"),
        ],
    )

    assert result.exit_code != 0
    assert "confirm" in result.output.lower()
    restore.assert_not_called()


def test_backup_restore_cli_passes_explicit_targets_and_redacts_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restore = Mock(return_value={"status": "restored", "backup_id": "b-1"})
    service = Mock()
    service.restore = restore
    monkeypatch.setattr(cli, "_build_backup_service", Mock(return_value=service))
    (tmp_path / "backup").mkdir()

    result = CliRunner().invoke(
        cli.app,
        [
            "ops",
            "backup",
            "restore",
            str(tmp_path / "backup"),
            "--target-database-url",
            "postgresql://user:restore-secret@localhost/app",
            "--target-checkpoint-database-url",
            "postgresql://user:checkpoint-secret@localhost/app",
            "--target-object-root",
            str(tmp_path / "objects"),
            "--confirm",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "restored"' in result.stdout
    assert "restore-secret" not in result.output
    assert "checkpoint-secret" not in result.output
    restore.assert_called_once_with(
        backup_dir=tmp_path / "backup",
        target_database_url="postgresql://user:restore-secret@localhost/app",
        target_checkpoint_database_url="postgresql://user:checkpoint-secret@localhost/app",
        target_object_root=tmp_path / "objects",
        confirmed=True,
    )


def test_s3_storage_accepts_default_and_explicit_region() -> None:
    default_settings = Settings(**valid_production_settings())
    explicit_settings = valid_production_settings()
    explicit_settings["s3_region"] = "eu-west-2"

    assert default_settings.s3_region == "us-east-1"
    assert Settings(**explicit_settings).s3_region == "eu-west-2"


def test_production_settings_require_nonempty_allowed_origins() -> None:
    settings = valid_production_settings()
    settings["allowed_origins"] = []

    with pytest.raises(ValidationError, match="allowed_origins"):
        Settings(**settings)


def test_production_settings_reject_wildcard_allowed_origins() -> None:
    settings = valid_production_settings()
    settings["allowed_origins"] = ["*"]

    with pytest.raises(ValidationError, match="allowed_origins"):
        Settings(**settings)


@pytest.mark.parametrize(
    "origin",
    (
        "https://user:password@bidscope.example.test",
        "https://bidscope.example.test/admin",
        "https://bidscope.example.test/?source=campaign",
        "https://bidscope.example.test/#section",
    ),
)
def test_production_settings_reject_allowed_origins_that_are_not_exact_origins(
    origin: str,
) -> None:
    settings = valid_production_settings()
    settings["allowed_origins"] = [origin]

    with pytest.raises(ValidationError, match="allowed_origins"):
        Settings(**settings)


def test_production_settings_accept_non_default_allowed_origin_port() -> None:
    settings = valid_production_settings()
    settings["allowed_origins"] = ["https://bidscope.example.test:8443"]

    result = Settings(**settings)

    assert [str(origin) for origin in result.allowed_origins] == [
        "https://bidscope.example.test:8443/"
    ]


def test_production_settings_require_nonempty_trusted_hosts() -> None:
    settings = valid_production_settings()
    settings["trusted_hosts"] = []

    with pytest.raises(ValidationError, match="trusted_hosts"):
        Settings(**settings)


def test_production_settings_reject_wildcard_trusted_hosts() -> None:
    settings = valid_production_settings()
    settings["trusted_hosts"] = ["*"]

    with pytest.raises(ValidationError, match="trusted_hosts"):
        Settings(**settings)


def test_production_settings_require_https_external_scheme() -> None:
    settings = valid_production_settings()
    settings["external_scheme"] = "http"

    with pytest.raises(ValidationError, match="external_scheme"):
        Settings(**settings)


def test_valid_production_settings_are_accepted() -> None:
    settings = Settings(**valid_production_settings())

    assert settings.app_mode == "production"
    assert settings.admin_token.get_secret_value() == "a" * 32
    assert settings.object_store_type == "s3"
    assert settings.s3_endpoint == "https://s3.example.test"
    assert settings.s3_bucket == "bidscope-prod"
    assert settings.s3_access_key.get_secret_value() == "test-access-key"
    assert settings.s3_secret_key.get_secret_value() == "test-secret-key"
    assert [str(origin) for origin in settings.allowed_origins] == [
        "https://bidscope.example.test/"
    ]
    assert settings.trusted_hosts == ["bidscope.example.test"]
    assert settings.external_scheme == "https"


def test_snapshots_import_applies_selector_policy_before_async_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot import must install the Windows selector policy before asyncio.run.

    ``snapshots import`` drives async SQLAlchemy/asyncpg through ``asyncio.run``.
    On Windows the default Proactor loop is incompatible with psycopg/asyncpg, so
    the selector policy must be applied *before* the event loop is created — the
    same contract already honoured by ``api serve`` and the scheduler commands.
    """
    call_order: list[str] = []
    original_asyncio_run = cli.asyncio.run

    async def fake_run_import(bundle: Path) -> object:
        del bundle
        call_order.append("run_import")
        return Mock(
            snapshot_bundle_id="bundle-1",
            status="imported",
            id="import-1",
        )

    def tracking_asyncio_run(main, *args, **kwargs):  # noqa: ANN001
        call_order.append("asyncio_run")
        return original_asyncio_run(main, *args, **kwargs)

    monkeypatch.setattr(cli, "_run_import", fake_run_import)
    monkeypatch.setattr(cli, "configure_windows_selector_event_loop_policy",
                        lambda: call_order.append("selector_policy"))
    monkeypatch.setattr(cli.asyncio, "run", tracking_asyncio_run)

    cli.snapshots_import(bundle=Path("bundle.zip"), json_output=False)

    # The policy is installed before the event loop is created by asyncio.run,
    # and the import coroutine only runs once that loop is driving it.
    assert call_order == ["selector_policy", "asyncio_run", "run_import"]
