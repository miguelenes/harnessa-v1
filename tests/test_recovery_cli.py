from __future__ import annotations

import argparse
import io
import json
import threading
import urllib.error
from collections.abc import Iterator
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs

import pytest
from harnessa.recovery_cli import HarnessaRecoveryClient, _confirm, _origin, _uuid


class _RecoveryHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _record(self) -> bytes:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        headers = {key.lower(): value for key, value in self.headers.items()}
        headers["cookie"] = ";".join(
            f"{key}={cookie.value}" for key, cookie in cookies.items()
        )
        self.requests.append((self.command, self.path, headers, body))
        return body

    def _respond(self, status: int, payload: object | None = None, *, headers: tuple[str, ...] = ()) -> None:
        self.send_response(status)
        for header in headers:
            self.send_header("Set-Cookie", header)
        if payload is None:
            self.end_headers()
            return
        content = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        self._record()
        if self.path == "/auth/csrf":
            self._respond(200, {"csrf_token": "test-csrf"}, headers=("harnessa_csrf=test-csrf; Path=/",))
        elif self.path == "/auth/me":
            self._respond(200, {"id": "operator-id", "email": "operator@example.test", "role": "operator"})
        elif self.path.endswith("/dead-letters"):
            self._respond(200, [{"id": "dead-id", "payload": {"secret": "must not print"}}])
        else:
            self._respond(404, {"detail": "missing"})

    def do_POST(self) -> None:
        body = self._record()
        if self.path == "/auth/cookie/login":
            assert parse_qs(body.decode()) == {
                "username": ["operator@example.test"], "password": ["not-logged"]
            }
            self._respond(204, headers=("harnessa_auth=opaque-session; HttpOnly; Path=/",))
        elif self.path == "/dead-letters/00000000-0000-0000-0000-000000000002/replay":
            self._respond(200, {"id": "task-id", "stage": "READY"})
        elif self.path == "/github/deliveries/00000000-0000-0000-0000-000000000002/retry":
            self._respond(202, {"status": "queued"})
        else:
            self._respond(404, {"detail": "missing"})


@pytest.fixture
def recovery_server() -> Iterator[str]:
    _RecoveryHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecoveryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_origin_allows_tls_and_loopback_only() -> None:
    assert _origin("https://harnessa.example.test/", "API") == "https://harnessa.example.test"
    assert _origin("http://localhost:8010", "API") == "http://localhost:8010"
    for value in (
        "http://harnessa.example.test",
        "https://operator:password@example.test",
        "https://example.test/api",
        "https://example.test?token=secret",
    ):
        with pytest.raises(ValueError):
            _origin(value, "API")


def test_uuid_rejects_untrusted_path_values() -> None:
    assert _uuid("00000000-0000-0000-0000-000000000001", "ID") == (
        "00000000-0000-0000-0000-000000000001"
    )
    with pytest.raises(argparse.ArgumentTypeError):
        _uuid("../../auth/me", "ID")


def test_login_and_recovery_keep_session_in_cookie_jar(recovery_server: str) -> None:
    client = HarnessaRecoveryClient(recovery_server, "http://localhost:3000")
    identity = client.login("operator@example.test", "not-logged")

    assert identity["id"] == "operator-id"
    assert {cookie.name for cookie in client.cookies} == {"harnessa_csrf", "harnessa_auth"}
    assert client.list_dead_letters("00000000-0000-0000-0000-000000000001")[0]["id"] == "dead-id"
    result = client.replay_dead_letter(
        "00000000-0000-0000-0000-000000000002", "Reviewed after external effect reconciliation"
    )

    assert result == {"id": "task-id", "stage": "READY"}
    login_request = _RecoveryHandler.requests[1]
    assert login_request[2]["origin"] == "http://localhost:3000"
    assert login_request[2]["x-csrf-token"] == "test-csrf"
    assert "harnessa_csrf=test-csrf" in login_request[2]["cookie"]
    replay_request = _RecoveryHandler.requests[-1]
    assert replay_request[2]["origin"] == "http://localhost:3000"
    assert replay_request[2]["x-csrf-token"] == "test-csrf"
    assert len(replay_request[2]["idempotency-key"]) == 36
    assert json.loads(replay_request[3]) == {
        "reason": "Reviewed after external effect reconciliation"
    }


def test_github_delivery_repair_uses_reason_and_role_gated_endpoint(recovery_server: str) -> None:
    client = HarnessaRecoveryClient(recovery_server, "http://localhost:3000")
    client.login("operator@example.test", "not-logged")
    result = client.retry_github_delivery("00000000-0000-0000-0000-000000000002", "Reviewed delivery")

    assert result == {"status": "queued"}
    request = _RecoveryHandler.requests[-1]
    assert request[0:2] == ("POST", "/github/deliveries/00000000-0000-0000-0000-000000000002/retry")
    assert request[2]["x-csrf-token"] == "test-csrf"
    assert json.loads(request[3]) == {"reason": "Reviewed delivery"}


def test_dead_letter_replay_validates_reason_before_request() -> None:
    client = HarnessaRecoveryClient("https://harnessa.example.test", "https://app.example.test")
    with pytest.raises(ValueError, match="Recovery reason"):
        client.replay_dead_letter("00000000-0000-0000-0000-000000000002", "  ")


def test_confirmation_mismatch_stops_before_any_mutation(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(RuntimeError, match="no recovery action was sent"):
        _confirm("00000000-0000-0000-0000-000000000002", input_fn=lambda _prompt: "different")
    assert "00000000-0000-0000-0000-000000000002" in capsys.readouterr().out


def test_http_error_body_is_not_exposed() -> None:
    class FailingOpener:
        def open(self, _request: object, *, timeout: float) -> object:
            del timeout
            raise urllib.error.HTTPError(
                "https://harnessa.example.test/failure",
                403,
                "denied",
                {"X-Request-ID": "safe-request-id"},
                io.BytesIO(b"credential-shaped-sensitive-body"),
            )

    client = HarnessaRecoveryClient(
        "https://harnessa.example.test",
        "https://app.example.test",
        opener=FailingOpener(),
    )
    with pytest.raises(RuntimeError) as error:
        client._request("GET", "/failure")
    assert "safe-request-id" in str(error.value)
    assert "credential-shaped-sensitive-body" not in str(error.value)
