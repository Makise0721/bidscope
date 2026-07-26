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
