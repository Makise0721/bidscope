"""Deployment-contract tests.

These assert the deployable-image intent by reading the Dockerfile and
compose.yaml as text. They do NOT require a running database or Docker daemon,
so they live under ``backend/tests/unit`` (the integration conftest gates its
tests behind a dedicated ``*_test`` database and ``alembic upgrade head``).

Reconciliation note: the plan's Task 5 Step 1 suggested asserting the literal
substring ``COPY alembic.ini migrations/ ./``. That single COPY is a Docker
anti-pattern: a directory source flattens its contents into the destination,
so migrations files would land in ``/app/`` rather than ``/app/migrations/``
and break alembic. We instead assert the deployable intent (alembic.ini
copied, migrations copied as a directory, postgres-init copied, canonical
serve command) which is what Step 4's two-line COPY template actually
specifies.

The file was relocated from ``backend/tests/integration/`` per the
remediation plan-vs-reality reconciliation: the integration conftest's
session-scoped autouse fixtures require ``BIDSCOPE_APP_MODE=test`` and a
``*_test`` database, neither of which a pure file-content contract test
needs.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_image_contains_migration_inputs_and_canonical_api_command() -> None:
    dockerfile = _read("Dockerfile")
    # alembic.ini is copied into the image (grouped with the project metadata
    # COPY); assert it appears as a COPY source rather than a literal
    # "COPY alembic.ini ..." prefix, since Step 4's template consolidates it
    # onto one line with pyproject.toml/uv.lock.
    copy_lines = [ln for ln in dockerfile.splitlines() if ln.startswith("COPY")]
    assert any("alembic.ini" in line for line in copy_lines)
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY scripts/postgres-init ./scripts/postgres-init" in dockerfile
    assert 'CMD ["bidscope", "api", "serve"' in dockerfile


def test_compose_api_uses_canonical_serve_command_and_has_minio_init() -> None:
    compose = _read("compose.yaml")
    # api service must invoke the canonical serve subcommand
    assert '"serve"' in compose
    # a one-shot minio bucket bootstrap service must exist and create the configured bucket
    assert "minio-init" in compose
    assert "mc mb" in compose


def test_compose_production_services_share_required_settings_without_literal_secrets() -> None:
    compose = _read("compose.yaml")

    assert "x-production-environment: &production-environment" in compose
    assert compose.count("<<: *production-environment") == 2
    assert "BIDSCOPE_APP_MODE: production" in compose
    assert "BIDSCOPE_OBJECT_STORE_TYPE: s3" in compose
    assert "BIDSCOPE_APP_MODE: demo" not in compose
    assert "minioadmin" not in compose

    required_keys = (
        "BIDSCOPE_ADMIN_TOKEN",
        "BIDSCOPE_ALLOWED_ORIGINS",
        "BIDSCOPE_TRUSTED_HOSTS",
        "BIDSCOPE_EXTERNAL_SCHEME",
        "BIDSCOPE_S3_ENDPOINT",
        "BIDSCOPE_S3_REGION",
        "BIDSCOPE_S3_BUCKET",
        "BIDSCOPE_S3_PREFIX",
        "BIDSCOPE_S3_ACCESS_KEY",
        "BIDSCOPE_S3_SECRET_KEY",
        "BIDSCOPE_REAL_MODEL_ENABLED",
        "BIDSCOPE_MODEL_API_KEY",
        "BIDSCOPE_MODEL_BASE_URL",
        "BIDSCOPE_MODEL_NAME",
    )
    for key in required_keys:
        assert f"{key}: ${{{key}:?" in compose or key in {
            "BIDSCOPE_REAL_MODEL_ENABLED",
            "BIDSCOPE_MODEL_API_KEY",
            "BIDSCOPE_MODEL_BASE_URL",
            "BIDSCOPE_MODEL_NAME",
        }
    assert "BIDSCOPE_REAL_MODEL_ENABLED: ${BIDSCOPE_REAL_MODEL_ENABLED:-false}" in compose
    assert "BIDSCOPE_MODEL_API_KEY: ${BIDSCOPE_MODEL_API_KEY-}" in compose
    assert (
        "BIDSCOPE_MODEL_BASE_URL: ${BIDSCOPE_MODEL_BASE_URL:-https://api.deepseek.com}"
        in compose
    )
    assert "BIDSCOPE_MODEL_NAME: ${BIDSCOPE_MODEL_NAME:-deepseek-chat}" in compose


def _service_block(compose: str, service_name: str) -> str:
    marker = f"  {service_name}:\n"
    start = compose.index(marker)
    next_service = re.search(r"\n  [A-Za-z][A-Za-z0-9_-]*:\n", compose[start + len(marker) :])
    if next_service is None:
        return compose[start:]
    return compose[start : start + len(marker) + next_service.start()]


def test_compose_keeps_database_and_object_storage_internal_and_scheduler_unchecked() -> None:
    compose = _read("compose.yaml")

    postgres = _service_block(compose, "postgres")
    minio = _service_block(compose, "minio")
    api = _service_block(compose, "api")
    scheduler = _service_block(compose, "scheduler")

    assert "ports:" not in postgres
    assert "ports:" not in minio
    assert '"127.0.0.1:8000:8000"' in api
    assert "healthcheck:" in api
    assert "healthcheck:\n      disable: true" in scheduler


def test_rendered_compose_keeps_infrastructure_unpublished(
    compose_environment: dict[str, str],
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")

    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=compose_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["minio"]
    assert len(services["api"]["ports"]) == 1
    api_port = services["api"]["ports"][0]
    assert {
        "host_ip": api_port["host_ip"],
        "published": api_port["published"],
        "target": api_port["target"],
    } == {"host_ip": "127.0.0.1", "published": "8000", "target": 8000}
    assert services["scheduler"]["healthcheck"]["disable"] is True
    for service_name in ("api", "scheduler"):
        environment = services[service_name]["environment"]
        assert environment["BIDSCOPE_MODEL_BASE_URL"] == "https://model.example.test/v1"
        assert environment["BIDSCOPE_MODEL_NAME"] == "model-sentinel"


def test_compose_requires_preencoded_postgres_dsns_and_keeps_service_credentials() -> None:
    compose = _read("compose.yaml")

    assert "POSTGRES_PASSWORD: bidscope" not in compose
    assert "bidscope:bidscope" not in compose
    for key in (
        "BIDSCOPE_POSTGRES_DB",
        "BIDSCOPE_POSTGRES_USER",
        "BIDSCOPE_POSTGRES_PASSWORD",
        "BIDSCOPE_DATABASE_URL",
        "BIDSCOPE_CHECKPOINT_DATABASE_URL",
    ):
        assert f"${{{key}:?" in compose
    assert 'pg_isready -U \\"$${POSTGRES_USER}\\" -d \\"$${POSTGRES_DB}\\"' in compose
    assert compose.count(
        "BIDSCOPE_DATABASE_URL: ${BIDSCOPE_DATABASE_URL:?set BIDSCOPE_DATABASE_URL to a "
        "pre-encoded PostgreSQL application DSN}"
    ) == 2
    assert compose.count(
        "BIDSCOPE_CHECKPOINT_DATABASE_URL: "
        "${BIDSCOPE_CHECKPOINT_DATABASE_URL:?set BIDSCOPE_CHECKPOINT_DATABASE_URL to a "
        "pre-encoded PostgreSQL checkpoint DSN}"
    ) == 2
    assert "${BIDSCOPE_POSTGRES_PASSWORD}@postgres" not in compose
    assert "postgresql+asyncpg://${BIDSCOPE_POSTGRES_USER}" not in compose
    assert "postgresql+psycopg://${BIDSCOPE_POSTGRES_USER}" not in compose


@pytest.fixture
def compose_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BIDSCOPE_ADMIN_TOKEN": "a" * 32,
            "BIDSCOPE_ALLOWED_ORIGINS": '["https://bidscope.example.test"]',
            "BIDSCOPE_TRUSTED_HOSTS": '["bidscope.example.test"]',
            "BIDSCOPE_EXTERNAL_SCHEME": "https",
            "BIDSCOPE_S3_ENDPOINT": "http://minio:9000",
            "BIDSCOPE_S3_REGION": "us-east-1",
            "BIDSCOPE_S3_BUCKET": "bidscope-prod",
            "BIDSCOPE_S3_PREFIX": "production/objects",
            "BIDSCOPE_S3_ACCESS_KEY": "bidscope-access",
            "BIDSCOPE_S3_SECRET_KEY": "bidscope-secret",
            "BIDSCOPE_POSTGRES_DB": "bidscope",
            "BIDSCOPE_POSTGRES_USER": "bidscope",
            "BIDSCOPE_POSTGRES_PASSWORD": "p@ss:word/?#[]",
            "BIDSCOPE_REAL_MODEL_ENABLED": "false",
            "BIDSCOPE_MODEL_API_KEY": "",
            "BIDSCOPE_MODEL_BASE_URL": "https://model.example.test/v1",
            "BIDSCOPE_MODEL_NAME": "model-sentinel",
            "BIDSCOPE_DATABASE_URL": (
                "postgresql+asyncpg://bidscope:p%40ss%3Aword%2F%3F%23%5B%5D"
                "@postgres:5432/bidscope"
            ),
            "BIDSCOPE_CHECKPOINT_DATABASE_URL": (
                "postgresql+psycopg://bidscope:p%40ss%3Aword%2F%3F%23%5B%5D"
                "@postgres:5432/bidscope"
            ),
        }
    )
    return environment


def test_compose_config_accepts_preencoded_postgres_dsns(
    compose_environment: dict[str, str],
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")

    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        env=compose_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "missing_key",
    ("BIDSCOPE_DATABASE_URL", "BIDSCOPE_CHECKPOINT_DATABASE_URL", "BIDSCOPE_S3_PREFIX"),
)
def test_compose_config_rejects_missing_required_production_value(
    compose_environment: dict[str, str],
    missing_key: str,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    compose_environment.pop(missing_key)

    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        env=compose_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert missing_key in result.stderr
