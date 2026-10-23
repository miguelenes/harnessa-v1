"""HTTP client for a separately permissioned Harness² holdout evaluator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx


class HTTPHarnessEvaluator:
    """Send frozen manifest references to an independently operated evaluator."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 300,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1",
        }
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (parsed.scheme != "https" and not loopback_http)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Holdout evaluator URL must use HTTPS or loopback HTTP")
        if not bearer_token.strip():
            raise ValueError("Holdout evaluator bearer token is required")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("Holdout evaluator timeout must be between 1 and 3600 seconds")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds
        self._transport = _transport

    async def evaluate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._endpoint,
                json=dict(candidate),
                headers={"Authorization": f"Bearer {self._bearer_token}"},
            )
        if response.is_error or response.is_redirect:
            # Do not include response bodies or endpoint credentials in exceptions.
            raise RuntimeError(f"Holdout evaluator returned HTTP {response.status_code}")
        try:
            receipt = response.json()
        except ValueError as exc:
            raise RuntimeError("Holdout evaluator returned invalid JSON") from exc
        if not isinstance(receipt, dict):
            raise TypeError("Holdout evaluator receipt must be a JSON object")
        return receipt
