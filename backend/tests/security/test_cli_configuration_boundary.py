"""Production startup must report invalid configuration without secret leakage."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DSN_PASSWORD = "cli-boundary-dsn-password-51f0"


def test_api_cli_masks_invalid_production_database_url_in_process_output() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "BIDSCOPE_APP_MODE": "production",
            "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
            "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
            "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
            "BIDSCOPE_S3_BUCKET": "bidscope-prod",
            "BIDSCOPE_S3_ACCESS_KEY": "access-key",
            "BIDSCOPE_S3_SECRET_KEY": "secret-key",
            "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
            "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
            "BIDSCOPE_EXTERNAL_SCHEME": "https",
            "BIDSCOPE_DATABASE_URL": (
                "postgresql+asyncpg://bidscope:"
                f"{DSN_PASSWORD}@database.example.test:5432/bidscope?host=override.example.test"
            ),
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": (
                "postgresql+psycopg://bidscope:checkpoint-password"
                "@database.example.test:5432/bidscope"
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "from bidscope.cli import api_serve; api_serve()"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert DSN_PASSWORD not in output
    assert "database.example.test" not in output


@pytest.mark.parametrize(
    "command",
    (
        ("checkpoints", "setup"),
        ("snapshots", "inspect", "."),
        ("snapshots", "import", "."),
    ),
)
def test_admin_cli_commands_mask_invalid_startup_settings(command: tuple[str, ...]) -> None:
    environment = os.environ.copy()
    malformed_dsn = "postgresql+asyncpg://bare-secret"
    environment.update(
        {
            "BIDSCOPE_APP_MODE": "production",
            "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
            "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
            "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
            "BIDSCOPE_S3_BUCKET": "bidscope-prod",
            "BIDSCOPE_S3_ACCESS_KEY": "access-key",
            "BIDSCOPE_S3_SECRET_KEY": "secret-key",
            "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
            "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
            "BIDSCOPE_EXTERNAL_SCHEME": "https",
            "BIDSCOPE_DATABASE_URL": malformed_dsn,
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": (
                "postgresql+psycopg://bidscope:checkpoint-password"
                "@database.example.test:5432/bidscope"
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "bidscope.cli", *command],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "BidScope startup configuration is invalid." in output
    assert "ValidationError" not in output
    assert "validation error for Settings" not in output
    assert malformed_dsn not in output
    assert "bare-secret" not in output


@pytest.mark.parametrize(
    "command",
    (
        ("checkpoints", "setup"),
        ("snapshots", "inspect", "."),
        ("snapshots", "import", "."),
    ),
)
def test_admin_cli_commands_bound_malformed_complex_startup_settings(
    command: tuple[str, ...]
) -> None:
    environment = os.environ.copy()
    malformed_origins = "not-json"
    environment.update(
        {
            "BIDSCOPE_APP_MODE": "production",
            "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
            "BIDSCOPE_OBJECT_STORE_TYPE": "s3",
            "BIDSCOPE_S3_ENDPOINT": "https://s3.example.test",
            "BIDSCOPE_S3_BUCKET": "bidscope-prod",
            "BIDSCOPE_S3_ACCESS_KEY": "access-key",
            "BIDSCOPE_S3_SECRET_KEY": "secret-key",
            "BIDSCOPE_ALLOWED_ORIGINS": malformed_origins,
            "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
            "BIDSCOPE_EXTERNAL_SCHEME": "https",
            "BIDSCOPE_DATABASE_URL": (
                "postgresql+asyncpg://bidscope:database-password"
                "@database.example.test:5432/bidscope"
            ),
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": (
                "postgresql+psycopg://bidscope:checkpoint-password"
                "@database.example.test:5432/bidscope"
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "bidscope.cli", *command],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "BidScope startup configuration is invalid." in output
    assert "SettingsError" not in output
    assert "JSONDecodeError" not in output
    assert malformed_origins not in output
