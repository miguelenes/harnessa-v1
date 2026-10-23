"""Narrow GitHub App REST adapter; credentials never cross its boundary."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from harnessa.config import settings

GITHUB_API_VERSION = "2022-11-28"
APP_REQUIRED_PERMISSIONS: dict[str, str] = {
    "checks": "read",
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "statuses": "write",
}
APP_ALLOWED_TOKEN_PERMISSIONS = {
    **APP_REQUIRED_PERMISSIONS,
    "metadata": "read",
}
_SECRET_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class GitHubAppError(RuntimeError):
    """Sanitized adapter error; response bodies and authentication headers are omitted."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def resolve_secret_reference(
    reference: str,
    *,
    references_json: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve an opaque deployment reference to an environment-held secret value."""
    if not _SECRET_REF.fullmatch(reference):
        raise GitHubAppError("GitHub secret reference is invalid")
    raw = references_json if references_json is not None else settings.github_secret_references_json
    try:
        references = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubAppError("GITHUB_SECRET_REFERENCES_JSON is invalid") from exc
    if not isinstance(references, dict):
        raise GitHubAppError("GITHUB_SECRET_REFERENCES_JSON must be an object")
    env_name = references.get(reference)
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise GitHubAppError("GitHub secret reference is not configured")
    value = (environ if environ is not None else os.environ).get(env_name, "")
    if not value:
        raise GitHubAppError("GitHub secret reference is unavailable")
    return value


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Validate X-Hub-Signature-256 against the exact request bytes."""
    import hashlib
    import hmac

    if not signature_header or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature_header):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def check_permissions(granted: Mapping[str, str], required: Mapping[str, str]) -> list[str]:
    levels = {"none": 0, "read": 1, "write": 2, "admin": 3}
    missing = []
    for permission, minimum in required.items():
        if levels.get(str(granted.get(permission, "none")), 0) < levels[minimum]:
            missing.append(f"{permission}:{minimum}")
    return missing


class GitHubAppClient:
    """Short-lived app JWTs and repository-scoped installation tokens over HTTPS."""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        references_json: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.api_url = (api_url or settings.github_api_url).rstrip("/")
        parts = urlsplit(self.api_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise GitHubAppError("GitHub API URL must be an HTTPS origin or HTTPS API prefix")
        self.timeout_seconds = timeout_seconds or settings.github_http_timeout_seconds
        self.transport = transport
        self.references_json = references_json
        self.environ = environ

    def _private_key(self, reference: str) -> str:
        return resolve_secret_reference(
            reference,
            references_json=self.references_json,
            environ=self.environ,
        ).replace("\\n", "\n")

    def app_jwt(self, app_id: int, private_key_reference: str) -> str:
        now = int(time.time())
        try:
            return str(
                jwt.encode(
                    {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                    self._private_key(private_key_reference),
                    algorithm="RS256",
                )
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            raise GitHubAppError("GitHub App private key is invalid") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self.api_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "Harnessa-GitHub-App",
        }
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                    follow_redirects=False,
                ) as client,
                client.stream(method, url, headers=headers, json=body, params=params) as res,
            ):
                content = bytearray()
                async for chunk in res.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise GitHubAppError("GitHub API response exceeded 2 MB")
                if res.status_code < 200 or res.status_code >= 300:
                    raise GitHubAppError(
                        f"GitHub API request failed with HTTP {res.status_code}",
                        status_code=res.status_code,
                    )
                if not content:
                    return {}
                value = json.loads(content)
                if not isinstance(value, (dict, list)):
                    raise GitHubAppError("GitHub API returned an unexpected response")
                return value
        except GitHubAppError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Don't forward exception details: httpx exceptions can retain request data.
            raise GitHubAppError(f"GitHub API transport failed ({type(exc).__name__})") from None

    async def get_installation(
        self, *, app_id: int, private_key_reference: str, installation_id: int
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/app/installations/{installation_id}",
            token=self.app_jwt(app_id, private_key_reference),
        )
        if not isinstance(response, dict):
            raise GitHubAppError("GitHub installation response is invalid")
        return response

    async def create_installation_token(
        self,
        *,
        app_id: int,
        private_key_reference: str,
        installation_id: int,
        repository_ids: list[int],
        permissions: Mapping[str, str],
    ) -> tuple[str, dict[str, Any]]:
        if (
            not repository_ids
            or len(repository_ids) > 100
            or any(value <= 0 for value in repository_ids)
        ):
            raise GitHubAppError("Installation tokens require 1 to 100 explicit repository IDs")
        requested = dict(permissions)
        if any(
            permission not in APP_ALLOWED_TOKEN_PERMISSIONS
            or level not in {"read", "write"}
            or (permission == "metadata" and level != "read")
            for permission, level in requested.items()
        ) or not requested:
            raise GitHubAppError("Requested GitHub permission levels are invalid")
        response = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self.app_jwt(app_id, private_key_reference),
            body={"repository_ids": repository_ids, "permissions": requested},
        )
        if not isinstance(response, dict):
            raise GitHubAppError("GitHub installation token response is invalid")
        token = response.get("token")
        permissions_result = response.get("permissions")
        repositories = response.get("repositories")
        if not isinstance(token, str) or not token or not isinstance(permissions_result, dict):
            raise GitHubAppError("GitHub installation token response omitted required fields")
        if check_permissions(permissions_result, requested):
            raise GitHubAppError("GitHub installation token lacks requested permissions")
        if not isinstance(repositories, list):
            raise GitHubAppError("GitHub did not confirm the requested repository token scope")
        returned_ids = {item.get("id") for item in repositories if isinstance(item, dict)}
        if not set(repository_ids).issubset(returned_ids):
            raise GitHubAppError("GitHub token was not scoped to the requested repository")
        return token, response

    async def request_installation(
        self,
        *,
        app_id: int,
        private_key_reference: str,
        installation_id: int,
        repository_id: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        permissions: Mapping[str, str],
    ) -> dict[str, Any] | list[Any]:
        token, _ = await self.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_reference,
            installation_id=installation_id,
            repository_ids=[repository_id],
            permissions=permissions,
        )
        return await self._request(method, path, token=token, body=body, params=params)
