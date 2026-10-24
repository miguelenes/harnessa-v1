"""Interactive, audited operator commands for Harnessa recovery endpoints."""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _origin(value: str, label: str) -> str:
    parsed = urlsplit(value.strip())
    loopback_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost", "127.0.0.1", "::1",
    }
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (parsed.scheme != "https" and not loopback_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTPS origin or a loopback HTTP origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a UUID") from exc


class HarnessaRecoveryClient:
    """Use an in-memory cookie jar and never put credentials in command arguments."""

    def __init__(
        self,
        api_origin: str,
        web_origin: str,
        *,
        opener: Any | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        self.api_origin = _origin(api_origin, "API origin")
        self.web_origin = _origin(web_origin, "Web origin")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("Timeout must be between 1 and 60 seconds")
        self.timeout_seconds = timeout_seconds
        self.cookies = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect()
        )
        self.csrf_token = ""

    def login(self, username: str, password: str) -> Mapping[str, Any]:
        csrf = self._request("GET", "/auth/csrf")
        if not isinstance(csrf, dict):
            raise TypeError("Harnessa returned an invalid CSRF response")
        token = csrf.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Harnessa did not return a CSRF token")
        self.csrf_token = token
        form = urllib.parse.urlencode({"username": username, "password": password}).encode()
        self._request("POST", "/auth/cookie/login", body=form, form=True)
        identity = self._request("GET", "/auth/me")
        if not isinstance(identity, dict) or not isinstance(identity.get("id"), str):
            raise TypeError("Harnessa did not return an authenticated identity")
        return identity

    def list_dead_letters(self, project_id: str) -> list[Mapping[str, Any]]:
        rows = self._request("GET", f"/projects/{_uuid(project_id, 'Project ID')}/dead-letters")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Harnessa returned an invalid dead-letter list")
        return rows

    def replay_dead_letter(self, dead_letter_id: str, reason: str) -> Mapping[str, Any]:
        identifier = _uuid(dead_letter_id, "Dead-letter ID")
        if not reason.strip() or len(reason) > 1000:
            raise ValueError("Recovery reason must contain 1 to 1000 characters")
        result = self._request(
            "POST",
            f"/dead-letters/{identifier}/replay",
            body=json.dumps({"reason": reason}).encode(),
            idempotency_key=str(uuid4()),
        )
        if not isinstance(result, dict):
            raise TypeError("Harnessa returned an invalid dead-letter recovery response")
        return result

    def retry_github_delivery(self, delivery_id: str, reason: str) -> Mapping[str, Any]:
        identifier = _uuid(delivery_id, "Delivery ID")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("Repair reason must contain 1 to 500 characters")
        result = self._request(
            "POST",
            f"/github/deliveries/{identifier}/retry",
            body=json.dumps({"reason": reason}).encode(),
        )
        if not isinstance(result, dict):
            raise TypeError("Harnessa returned an invalid GitHub delivery repair response")
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        form: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded" if form else "application/json"
            )
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.update({"Origin": self.web_origin, "X-CSRF-Token": self.csrf_token})
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.api_origin + path, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            request_id = exc.headers.get("X-Request-ID", "unavailable")
            raise RuntimeError(
                f"Harnessa request failed with HTTP {exc.code}; request ID {request_id}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError("Could not reach the configured Harnessa API origin") from None
        if not content:
            return {}
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Harnessa returned an invalid JSON response") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-origin", required=True, help="Harnessa API HTTPS origin")
    parser.add_argument("--web-origin", required=True, help="Allowed Harnessa web HTTPS origin")
    commands = parser.add_subparsers(dest="command", required=True)

    dead_letters = commands.add_parser("dead-letters", help="list recoverable task dead letters")
    dead_letters.add_argument("--project-id", required=True, type=lambda v: _uuid(v, "Project ID"))

    replay = commands.add_parser("replay-dead-letter", help="audit and replay one dead letter")
    replay.add_argument(
        "--dead-letter-id", required=True, type=lambda v: _uuid(v, "Dead-letter ID")
    )

    retry = commands.add_parser("retry-github-delivery", help="retry one failed GitHub delivery")
    retry.add_argument("--delivery-id", required=True, type=lambda v: _uuid(v, "Delivery ID"))
    return parser


def _confirm(identifier: str, *, input_fn: Callable[[str], str] = input) -> None:
    print(f"This action can dispatch work or repeat GitHub delivery processing: {identifier}")
    if input_fn("Type the exact UUID to continue: ").strip() != identifier:
        raise RuntimeError("Confirmation did not match; no recovery action was sent")


def _run(args: argparse.Namespace) -> int:
    client = HarnessaRecoveryClient(args.api_origin, args.web_origin)
    username = input("Harnessa email: ").strip()
    password = getpass.getpass("Harnessa password: ")
    identity = client.login(username, password)
    print(f"Authenticated as {identity.get('email', 'user')} ({identity.get('role', 'member')})")

    if args.command == "dead-letters":
        rows = client.list_dead_letters(args.project_id)
        print("Dead letters (payload and error details are omitted):")
        for row in rows:
            fields = {key: row.get(key) for key in ("id", "task_id", "run_id", "kind", "status")}
            print(json.dumps(fields, sort_keys=True))
        return 0

    if args.command == "replay-dead-letter":
        _confirm(args.dead_letter_id)
        reason = input("Audit reason (1–1000 characters): ").strip()
        result = client.replay_dead_letter(args.dead_letter_id, reason)
        print(json.dumps({"task_id": result.get("id"), "stage": result.get("stage")}))
        return 0

    _confirm(args.delivery_id)
    reason = input("Audit reason (1–500 characters): ").strip()
    result = client.retry_github_delivery(args.delivery_id, reason)
    print(json.dumps({"status": result.get("status", "unknown")}))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (TypeError, ValueError, RuntimeError) as exc:
        print(f"Recovery command failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
