from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from harnessa.adapters.mcp_http import MCPRemoteError, StreamableHTTPMCPClient
from harnessa.api.retrieval_routes import _validate_tool_arguments
from harnessa.services.retrieval_governance import MODERN_MCP_PROTOCOL, MCPGrant, invoke_mcp_tool


@pytest.mark.anyio
async def test_streamable_http_mcp_negotiates_discovers_and_invokes_with_secret_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", "fixture-only-secret")
    calls: list[tuple[str, str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        method = payload["method"]
        calls.append((method, request.headers.get("MCP-Protocol-Version", ""), request.headers.get("Authorization")))
        if method == "initialize":
            response: dict[str, object] = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            }
            return httpx.Response(200, json=response, headers={"Mcp-Session-Id": "fixture-session"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "read_status",
                                "description": "Read-only fixture",
                                "inputSchema": {"type": "object", "properties": {}, "required": []},
                            }
                        ]
                    },
                },
            )
        assert method == "tools/call"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"content": [{"type": "text", "text": "untrusted"}]}}
        )

    client = StreamableHTTPMCPClient(
        {"fixture": "https://mcp.test"},
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    tools = await client.discover("fixture", "2025-11-25", "TEST_MCP_TOKEN")
    assert [tool.name for tool in tools] == ["read_status"]
    result = await invoke_mcp_tool(
        client,
        grant=MCPGrant(
            "org", "project", "fixture", frozenset({"read_status"}), "2025-11-25", "TEST_MCP_TOKEN"
        ),
        authenticated_project_id="project",
        discovered_tool=type(tools[0])(**{**tools[0].__dict__, "read_only": True}),
        arguments={},
        server_protocol_version="2025-11-25",
    )
    assert json.loads(result)["content"][0]["text"] == "untrusted"
    assert [method for method, _, _ in calls].count("initialize") == 2
    assert all(version == "2025-11-25" for _, version, _ in calls if version)
    assert all(auth == "Bearer fixture-only-secret" for _, _, auth in calls)


@pytest.mark.anyio
async def test_streamable_http_mcp_fails_closed_on_protocol_mismatch_output_overflow_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mismatch(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-06-18"}},
        )

    client = StreamableHTTPMCPClient(
        {"fixture": "https://mcp.test"}, transport_factory=lambda: httpx.MockTransport(mismatch)
    )
    with pytest.raises(MCPRemoteError, match="outside the project grant"):
        await client.discover("fixture", "2025-11-25", None)

    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 64_001)

    overflow_client = StreamableHTTPMCPClient(
        {"fixture": "https://mcp.test"}, transport_factory=lambda: httpx.MockTransport(oversized)
    )
    with pytest.raises(MCPRemoteError, match="64,000 bytes"):
        await overflow_client.discover("fixture", "2025-11-25", None)

    monkeypatch.delenv("TEST_MCP_MISSING", raising=False)
    secret_client = StreamableHTTPMCPClient({"fixture": "https://mcp.test"})
    with pytest.raises(MCPRemoteError, match="credential reference is unavailable"):
        await secret_client.discover("fixture", "2025-11-25", "TEST_MCP_MISSING")


def test_mcp_argument_validation_checks_nested_values_and_rejects_unsupported_schemas() -> None:
    schema = {
        "type": "object",
        "properties": {
            "request": {
                "type": "object",
                "required": ["limit"],
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            },
            "labels": {"type": "array", "items": {"type": "string", "enum": ["safe", "read"]}},
        },
        "required": ["request"],
    }
    _validate_tool_arguments(schema, {"request": {"limit": 5}, "labels": ["safe"]})
    with pytest.raises(HTTPException) as error:
        _validate_tool_arguments(schema, {"request": {"limit": 99}})
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        _validate_tool_arguments(schema, {"request": {"limit": True}})
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        _validate_tool_arguments(schema, {"request": {"limit": 2, "extra": "no"}})
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        _validate_tool_arguments({"type": "object", "$ref": "https://example.invalid/schema"}, {})
    assert error.value.status_code == 502


@pytest.mark.anyio
async def test_modern_mcp_uses_stateless_discovery_envelope_and_routing_headers() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        method = payload["method"]
        methods.append(method)
        assert request.headers["MCP-Protocol-Version"] == MODERN_MCP_PROTOCOL
        assert request.headers["Mcp-Method"] == method
        params = payload.get("params", {})
        assert params["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN_MCP_PROTOCOL
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": [MODERN_MCP_PROTOCOL],
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": [
                    {
                        "name": "read_status",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            }
        else:
            assert method == "tools/call"
            assert request.headers["Mcp-Name"] == "read_status"
            result = {"resultType": "complete", "content": [{"type": "text", "text": "fixture"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    client = StreamableHTTPMCPClient(
        {"fixture": "https://mcp.test"}, transport_factory=lambda: httpx.MockTransport(handler)
    )
    tools = await client.discover("fixture", MODERN_MCP_PROTOCOL, None)
    assert [tool.name for tool in tools] == ["read_status"]
    result = await client.invoke("fixture", "read_status", {}, MODERN_MCP_PROTOCOL, None)
    assert json.loads(result)["content"][0]["text"] == "fixture"
    assert methods == ["server/discover", "tools/list", "server/discover", "tools/call"]
