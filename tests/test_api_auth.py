from fastapi.testclient import TestClient
from harnessa.config import settings
from harnessa.main import app


def test_authenticated_routes_require_a_user():
    with TestClient(app) as client:
        response = client.get("/projects")
        assert response.status_code == 401


def test_mutations_reject_unapproved_origins_before_authentication():
    with TestClient(app) as client:
        response = client.post(
            "/projects",
            headers={"Origin": "https://attacker.invalid"},
            json={"name": "Nope", "slug": "nope"},
        )
        assert response.status_code == 403


def test_login_requires_origin_and_csrf_proof():
    allowed_origin = settings.allowed_origins.split(",")[0].strip()
    with TestClient(app) as client:
        response = client.post(
            "/auth/cookie/login",
            headers={"Origin": allowed_origin},
            data={"username": "someone@example.com", "password": "not-a-real-password"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_failed"
