from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import cast

import pytest
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    ToolCallLocation,
    ToolCallUpdate,
)
from harnessa.adapters.acp_session import (
    ACPAgentSession,
    ACPProtocolError,
    BoundedStdioTransport,
    ProtocolLimitExceeded,
)
from harnessa.adapters.agent_registry import get_agent
from harnessa.adapters.sandbox_policy import (
    SandboxPolicyError,
    permission_allowed,
    resolve_workspace_path,
)


class _Writer:
    def __init__(self) -> None:
        self.output = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.output.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.anyio
async def test_bounded_transport_rejects_malformed_oversized_and_idle_frames(
    tmp_path: Path,
) -> None:
    malformed_reader = asyncio.StreamReader(limit=128)
    malformed_reader.feed_data(b"{invalid json}\n")
    malformed = BoundedStdioTransport(
        malformed_reader, cast(object, _Writer()), max_frame_bytes=128, idle_timeout_seconds=0.1
    )
    with pytest.raises(ACPProtocolError, match="malformed"):
        await malformed.receive()

    oversized_reader = asyncio.StreamReader(limit=16)
    oversized_reader.feed_data(b"x" * 32 + b"\n")
    oversized = BoundedStdioTransport(
        oversized_reader, cast(object, _Writer()), max_frame_bytes=16, idle_timeout_seconds=0.1
    )
    with pytest.raises(ProtocolLimitExceeded, match="frame"):
        await oversized.receive()

    idle = BoundedStdioTransport(
        asyncio.StreamReader(),
        cast(object, _Writer()),
        max_frame_bytes=128,
        idle_timeout_seconds=0.01,
    )
    with pytest.raises(TimeoutError, match="idle"):
        await idle.receive()


@pytest.mark.anyio
async def test_fixture_agent_permission_resume_and_redacted_update_contract() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.acp_fixture_agent",
        cwd=Path.cwd(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024 + 1,
    )
    session = ACPAgentSession(process, max_frame_bytes=1024 * 1024, max_updates=10)
    try:
        initialized = await session.initialize()
        assert initialized.protocol_version == 1
        assert not initialized.agent_capabilities.session_capabilities.resume
        await session.new_session()
        assert await session.prompt("fixture permissions", timeout_seconds=5) == "end_turn"
        assert session.client.update_counts == {"agent_thought_chunk": 1}
        assert not hasattr(session.client, "thought_text")
    finally:
        await session.close()

    client = session.client
    options = [PermissionOption(option_id="once", name="Allow once", kind="allow_once")]
    safe = await client.request_permission(
        "s",
        ToolCallUpdate(
            tool_call_id="read",
            kind="read",
            locations=[ToolCallLocation(path="/workspace/README.md")],
        ),
        options,
    )
    denied = await client.request_permission(
        "s",
        ToolCallUpdate(
            tool_call_id="escape",
            kind="read",
            locations=[ToolCallLocation(path="/etc/passwd")],
        ),
        options,
    )
    assert isinstance(safe.outcome, AllowedOutcome)
    assert isinstance(denied.outcome, DeniedOutcome)
    assert get_agent("opencode").resume is True
    assert get_agent("opencode").package_version == "2.0.32"
    assert get_agent("opencode").supported_versions == ("2.0.32", "1.19.4")


@pytest.mark.anyio
async def test_fixture_agent_cancel_terminates_owned_process() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.acp_fixture_agent",
        cwd=Path.cwd(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024 + 1,
    )
    session = ACPAgentSession(process, max_frame_bytes=1024 * 1024, max_updates=10)
    try:
        await session.initialize()
        await session.new_session()
        prompt = asyncio.create_task(session.prompt("wait for cancellation", timeout_seconds=5))
        await asyncio.sleep(0.05)
        await session.cancel()
        assert await prompt == "cancelled"
    finally:
        await session.close()
    assert process.returncode is not None


@pytest.mark.anyio
async def test_prompt_timeout_closes_fixture_process() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.acp_fixture_agent",
        cwd=Path.cwd(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024 + 1,
    )
    session = ACPAgentSession(process, max_frame_bytes=1024 * 1024, max_updates=10)
    try:
        await session.initialize()
        await session.new_session()
        with pytest.raises(TimeoutError, match="run deadline"):
            await session.prompt("wait for cancellation", timeout_seconds=0.01)
    finally:
        await session.close()
    assert process.returncode is not None


@pytest.mark.anyio
async def test_crashed_process_and_protocol_path_traversal_are_rejected(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.exit(23)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4096,
    )
    session = ACPAgentSession(
        process, max_frame_bytes=4096, max_updates=10, idle_timeout_seconds=0.2
    )
    with pytest.raises(ACPProtocolError):
        await session.initialize()
    await session.close()
    assert process.returncode == 23

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape").symlink_to(tmp_path / "outside")
    assert resolve_workspace_path(workspace, "/workspace/file.txt") == workspace / "file.txt"
    with pytest.raises(SandboxPolicyError):
        resolve_workspace_path(workspace, "../outside")
    with pytest.raises(SandboxPolicyError):
        resolve_workspace_path(workspace, "/etc/passwd")
    with pytest.raises(SandboxPolicyError):
        resolve_workspace_path(workspace, "/workspace/escape/secret")
    assert not permission_allowed("read", ["/workspace/escape/secret"], workspace)
    assert not permission_allowed("read", ["/workspace/../etc/passwd"], workspace)
    assert permission_allowed("execute", [])
    assert not permission_allowed("fetch", ["/workspace/file.txt"])
