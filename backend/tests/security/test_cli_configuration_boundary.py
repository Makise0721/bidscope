"""Production startup must report invalid configuration without secret leakage."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
