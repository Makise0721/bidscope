"""CLI runtime compatibility tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from bidscope import cli
from bidscope.config import Settings
from pydantic import ValidationError


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
