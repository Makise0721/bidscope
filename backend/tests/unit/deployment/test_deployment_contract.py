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

from pathlib import Path

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
        "BIDSCOPE_S3_ACCESS_KEY",
        "BIDSCOPE_S3_SECRET_KEY",
    )
    for key in required_keys:
        assert f"{key}: ${{{key}:?" in compose


def test_compose_requires_interpolated_postgres_credentials_everywhere() -> None:
    compose = _read("compose.yaml")

    assert "POSTGRES_PASSWORD: bidscope" not in compose
    assert "bidscope:bidscope" not in compose
    for key in (
        "BIDSCOPE_POSTGRES_DB",
        "BIDSCOPE_POSTGRES_USER",
        "BIDSCOPE_POSTGRES_PASSWORD",
    ):
        assert f"${{{key}:?" in compose
    assert 'pg_isready -U \\"$${POSTGRES_USER}\\" -d \\"$${POSTGRES_DB}\\"' in compose
    assert (
        "postgresql+asyncpg://${BIDSCOPE_POSTGRES_USER}:"
        "${BIDSCOPE_POSTGRES_PASSWORD}@postgres:5432/${BIDSCOPE_POSTGRES_DB}"
    ) in compose
    assert (
        "postgresql+psycopg://${BIDSCOPE_POSTGRES_USER}:"
        "${BIDSCOPE_POSTGRES_PASSWORD}@postgres:5432/${BIDSCOPE_POSTGRES_DB}"
    ) in compose
