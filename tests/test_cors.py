from fastapi.testclient import TestClient
from harnessa.config import settings
from harnessa.main import app


def test_private_network_preflight_is_limited_to_configured_origins() -> None:
    allowed_origin = next(
        origin.strip() for origin in settings.allowed_origins.split(",") if origin.strip()
    )
    headers = {
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "content-type",
        "Access-Control-Request-Private-Network": "true",
    }

    with TestClient(app) as client:
        allowed = client.options(
            "/projects/example/models/budget",
            headers={**headers, "Origin": allowed_origin},
        )
        denied = client.options(
            "/projects/example/models/budget",
            headers={**headers, "Origin": "https://attacker.invalid"},
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == allowed_origin
    assert allowed.headers["access-control-allow-private-network"] == "true"
    assert denied.status_code == 400
    assert denied.headers.get("access-control-allow-origin") != "https://attacker.invalid"
