from bidscope.config import Settings
from bidscope.main import create_app
from fastapi.testclient import TestClient


def test_health_reports_demo_mode_without_startup_dependencies() -> None:
    # This route only reads app settings, so do not enter TestClient's context
    # manager and trigger the full database/checkpoint lifespan in a unit test.
    client = TestClient(create_app(Settings(app_mode="demo")))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo"}


def test_production_health_requires_an_allowed_host() -> None:
    settings = Settings(
        app_mode="production",
        admin_token="production-admin-token-sentinel-0123456789",
        object_store_type="s3",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bidscope-test",
        s3_access_key="test-access-key-sentinel",
        s3_secret_key="test-secret-key-sentinel",
        allowed_origins=["https://console.example.test"],
        trusted_hosts=["bidscope.example.test"],
        external_scheme="https",
        database_url=(
            "postgresql+asyncpg://bidscope:database-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
        checkpoint_database_url=(
            "postgresql+psycopg://bidscope:checkpoint-test-sentinel"
            "@database.example.test:5432/bidscope"
        ),
    )
    client = TestClient(create_app(settings), base_url="https://bidscope.example.test")
    try:
        allowed_response = client.get("/healthz")
        assert allowed_response.status_code == 200
        assert allowed_response.json() == {"status": "ok", "mode": "production"}

        invalid_host_response = client.get(
            "/healthz", headers={"Host": "other.example.test"}
        )
        assert invalid_host_response.status_code == 400
    finally:
        client.close()
