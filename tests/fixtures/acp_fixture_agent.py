"""Deterministic, offline ACP agent used by protocol conformance tests."""

from __future__ import annotations

import asyncio
from typing import Any

from acp import run_agent
from acp.schema import (
    AgentCapabilities,
    AgentThoughtChunk,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SessionCapabilities,
    ToolCallLocation,
    ToolCallUpdate,
)


class FixtureAgent:
    def __init__(self) -> None:
        self.connection: Any = None
        self.cancelled = asyncio.Event()

    def on_connect(self, connection: Any) -> None:
        self.connection = connection

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                session_capabilities=SessionCapabilities(),
            ),
            agent_info=Implementation(name="harnessa-fixture", version="0.1.0"),
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        del cwd, mcp_servers, kwargs
        self.cancelled.clear()
        return NewSessionResponse(session_id="fixture-session")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        del kwargs
        text = getattr(prompt[0], "text", "") if prompt else ""
        if text == "wait for cancellation":
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")

        options = [PermissionOption(option_id="once", name="Allow once", kind="allow_once")]
        safe = await self.connection.request_permission(
            session_id,
            ToolCallUpdate(
                tool_call_id="safe-read",
                kind="read",
                locations=[ToolCallLocation(path="/workspace/README.md")],
            ),
            options,
        )
        denied = await self.connection.request_permission(
            session_id,
            ToolCallUpdate(
                tool_call_id="outside-read",
                kind="read",
                locations=[ToolCallLocation(path="/etc/passwd")],
            ),
            options,
        )
        if safe.outcome.outcome != "selected" or denied.outcome.outcome != "cancelled":
            return PromptResponse(stop_reason="cancelled")
        await self.connection.session_update(
            session_id,
            AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content={"type": "text", "text": "fixture private thought"},
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del session_id, kwargs
        self.cancelled.set()


if __name__ == "__main__":
    asyncio.run(run_agent(FixtureAgent(), stdio_buffer_limit_bytes=1024 * 1024))
