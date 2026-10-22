"""Bounded Streamable HTTP MCP client for deployment-allowlisted servers."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from harnessa.services.retrieval_governance import (
    MODERN_MCP_PROTOCOL,
    SUPPORTED_MCP_PROTOCOLS,
    MCPTool,
)


class MCPRemoteError(RuntimeError):
    """Remote protocol or transport failure without response-content logging."""


class StreamableHTTPMCPClient:
    def __init__(
        self,
        servers: dict[str, str],
        *,
        timeout_seconds: float = 15.0,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._servers = servers
        self._timeout = min(max(timeout_seconds, 0.1), 15.0)
        self._transport_factory = transport_factory

    async def discover(self, server_id: str, protocol_version: str, secret_reference: str | None) -> tuple[MCPTool, ...]:
        result, headers = await self._initialize(server_id, protocol_version, secret_reference)
        if protocol_version != MODERN_MCP_PROTOCOL and result.get("protocolVersion") != protocol_version:
            raise MCPRemoteError("MCP server negotiated an unexpected protocol version")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise MCPRemoteError("MCP server did not advertise tool capability")
        if protocol_version != MODERN_MCP_PROTOCOL:
            await self._post(
                server_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers,
                secret_reference,
                allow_notification=True,
            )
        response, _ = await self._post(
            server_id,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": self._modern_params(protocol_version),
            },
            headers,
            secret_reference,
        )
        if protocol_version == MODERN_MCP_PROTOCOL and response.get("resultType") != "complete":
            raise MCPRemoteError("MCP discovery returned an unsupported incomplete result")
        tools = response.get("tools", [])
        if not isinstance(tools, list) or len(tools) > 256:
            raise MCPRemoteError("MCP server returned an invalid or oversized tool list")
        discovered: list[MCPTool] = []
        for raw in tools:
            if not isinstance(raw, dict):
                continue
            name, description = raw.get("name"), raw.get("description", "")
            schema = raw.get("inputSchema", {})
            if not isinstance(name, str) or not name or not isinstance(schema, dict):
                continue
            discovered.append(
                MCPTool(
                    name=name,
                    description=description[:500] if isinstance(description, str) else "",
                    input_schema=schema,
                    read_only=False,
                )
            )
        return tuple(discovered)

    async def invoke(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
        protocol_version: str,
        secret_reference: str | None,
    ) -> bytes:
        _, headers = await self._initialize(server_id, protocol_version, secret_reference)
        if protocol_version != MODERN_MCP_PROTOCOL:
            await self._post(
                server_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers,
                secret_reference,
                allow_notification=True,
            )
        response, _ = await self._post(
            server_id,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    **self._modern_params(protocol_version),
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
            headers,
            secret_reference,
        )
        if protocol_version == MODERN_MCP_PROTOCOL and response.get("resultType") != "complete":
            raise MCPRemoteError("MCP tool call returned an unsupported asynchronous result")
        return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    async def _initialize(
        self, server_id: str, protocol_version: str, secret_reference: str | None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if protocol_version not in SUPPORTED_MCP_PROTOCOLS:
            raise MCPRemoteError("MCP protocol version is not supported")
        if protocol_version == MODERN_MCP_PROTOCOL:
            result, response_headers = await self._post(
                server_id,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": self._modern_params(protocol_version),
                },
                {"MCP-Protocol-Version": protocol_version},
                secret_reference,
            )
            advertised = result.get("supportedVersions")
            if not isinstance(advertised, list) or protocol_version not in advertised:
                raise MCPRemoteError("MCP server does not advertise the granted protocol version")
            if result.get("resultType") != "complete":
                raise MCPRemoteError("MCP discovery returned an incomplete result")
            return result, {"MCP-Protocol-Version": protocol_version}
        response, response_headers = await self._post(
            server_id,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "Harnessa", "version": "0.1.0"},
                },
            },
            {},
            secret_reference,
        )
        result = response
        if not isinstance(result, dict):
            raise MCPRemoteError("MCP server did not return initialize capabilities")
        negotiated = result.get("protocolVersion")
        if not isinstance(negotiated, str):
            raise MCPRemoteError("MCP server omitted the negotiated protocol version")
        if negotiated != protocol_version:
            raise MCPRemoteError("MCP server negotiated a protocol version outside the project grant")
        session_id = response_headers.get("mcp-session-id")
        headers = {"MCP-Protocol-Version": negotiated}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return result, headers

    @staticmethod
    def _modern_params(protocol_version: str) -> dict[str, object]:
        if protocol_version != MODERN_MCP_PROTOCOL:
            return {}
        return {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": protocol_version,
                "io.modelcontextprotocol/clientInfo": {"name": "Harnessa", "version": "0.1.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }

    async def _post(
        self,
        server_id: str,
        payload: dict[str, object],
        protocol_headers: dict[str, str],
        secret_reference: str | None,
        *,
        allow_notification: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        endpoint = self._servers.get(server_id)
        if endpoint is None:
            raise MCPRemoteError("MCP server is not deployment-allowlisted")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise MCPRemoteError("MCP server endpoint is invalid")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise MCPRemoteError("non-loopback MCP endpoints require HTTPS")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **protocol_headers,
        }
        if protocol_headers.get("MCP-Protocol-Version") == MODERN_MCP_PROTOCOL:
            headers["Mcp-Method"] = str(payload.get("method", ""))
            params = payload.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                headers["Mcp-Name"] = str(params["name"])
        if secret_reference:
            if not secret_reference.replace("_", "").isalnum() or not secret_reference[0].isalpha():
                raise MCPRemoteError("MCP secret reference must be an environment variable name")
            token = os.environ.get(secret_reference)
            if not token:
                raise MCPRemoteError("MCP credential reference is unavailable")
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport_factory() if self._transport_factory else None,
            ) as client, client.stream(
                "POST", endpoint, headers=headers, json=payload
            ) as response:
                if allow_notification and response.status_code in {200, 202, 204}:
                    body = b""
                else:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise MCPRemoteError(f"MCP HTTP status {response.status_code}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 64_000:
                            raise MCPRemoteError("MCP protocol response exceeds 64,000 bytes")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                if not body:
                    return {}, dict(response.headers)
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    body = _extract_sse_data(body)
                parsed_body = json.loads(body)
                if not isinstance(parsed_body, dict):
                    raise MCPRemoteError("MCP response must be a JSON object")
                error = parsed_body.get("error")
                if isinstance(error, dict):
                    raise MCPRemoteError(f"MCP JSON-RPC error {error.get('code', 'unknown')}")
                result = parsed_body.get("result", {})
                if not isinstance(result, dict):
                    raise MCPRemoteError("MCP JSON-RPC result must be an object")
                return result, dict(response.headers)
        except httpx.HTTPError as exc:
            raise MCPRemoteError(f"MCP transport failed ({type(exc).__name__})") from exc


def _extract_sse_data(body: bytes) -> bytes:
    for line in body.splitlines():
        if line.startswith(b"data:"):
            return line[5:].strip()
    raise MCPRemoteError("MCP stream did not include a data event")
