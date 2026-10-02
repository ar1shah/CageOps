from fastapi.testclient import TestClient

from cageops_api.main import app


def test_healthz():
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
