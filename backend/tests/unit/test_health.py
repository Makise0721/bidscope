from bidscope.config import Settings
from bidscope.main import create_app
from fastapi.testclient import TestClient


def test_health_reports_demo_mode() -> None:
    # Inject explicit settings so the assertion does not depend on the
    # process-level environment (integration tests set app_mode=test).
    with TestClient(create_app(Settings(app_mode="demo"))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo"}
