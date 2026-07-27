"""CLI runtime compatibility tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from bidscope import cli
from bidscope.config import Settings
from pydantic import ValidationError


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


def test_settings_default_to_safe_nonproduction_configuration() -> None:
    settings = Settings()

    assert settings.admin_token_min_length == 32
    assert settings.allowed_origins == []
    assert settings.trusted_hosts == []
    assert settings.external_scheme == "http"
    assert settings.s3_region == "us-east-1"


def test_admin_token_min_length_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="admin_token_min_length"):
        Settings(admin_token_min_length=0)


def test_production_settings_reject_placeholder_admin_token() -> None:
    settings = valid_production_settings()
    settings["admin_token"] = "change-me"

    with pytest.raises(ValidationError, match="placeholder") as error:
        Settings(**settings)

    assert "change-me" not in str(error.value)


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
    assert settings.admin_token == "a" * 32
    assert settings.object_store_type == "s3"
    assert settings.s3_endpoint == "https://s3.example.test"
    assert settings.s3_bucket == "bidscope-prod"
    assert settings.s3_access_key == "test-access-key"
    assert settings.s3_secret_key == "test-secret-key"
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
