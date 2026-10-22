"""Bounded ACP stdio session built on the official Python SDK client connection."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.interfaces import Client
from acp.schema import (
    AcpMcpServer,
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    SseMcpServer,
    ToolCallUpdate,
)

from harnessa.adapters.sandbox_policy import permission_allowed

logger = logging.getLogger("harnessa.acp_session")


class ACPProtocolError(RuntimeError):
    """Malformed or over-budget ACP transport data was rejected."""


class ProtocolLimitExceeded(ACPProtocolError):
    """An ACP frame or update count exceeded its configured bound."""


class BoundedStdioTransport:
    """Official SDK transport with strict frame, byte, and message bounds."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_frame_bytes: int,
        idle_timeout_seconds: float,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_messages: int = 30_000,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.max_frame_bytes = max_frame_bytes
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_total_bytes = max_total_bytes
        self.max_messages = max_messages
        self.total_bytes = 0
        self.message_count = 0
        self.closed = False

    def _count_message(self, size: int) -> None:
        self.total_bytes += size
        self.message_count += 1
        if self.total_bytes > self.max_total_bytes:
            raise ProtocolLimitExceeded("ACP session exceeded its total byte limit")
        if self.message_count > self.max_messages:
            raise ProtocolLimitExceeded("ACP session exceeded its message limit")

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionError("ACP transport is closed")
        try:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ACPProtocolError("ACP SDK produced a non-JSON message") from exc
        if len(encoded) + 1 > self.max_frame_bytes:
            raise ProtocolLimitExceeded("ACP outbound frame exceeded the configured limit")
        self._count_message(len(encoded) + 1)
        self.writer.write(encoded + b"\n")
        await self.writer.drain()

    async def receive(self) -> dict[str, Any] | None:
        if self.closed:
            return None
        try:
            line = await asyncio.wait_for(
                self.reader.readuntil(b"\n"), timeout=self.idle_timeout_seconds
            )
        except asyncio.LimitOverrunError as exc:
            raise ProtocolLimitExceeded("ACP inbound frame exceeded the configured limit") from exc
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise ACPProtocolError("ACP process exited with a partial JSON-RPC frame") from exc
            return None
        except TimeoutError as exc:
            raise TimeoutError("ACP process exceeded the idle protocol deadline") from exc
        if len(line) > self.max_frame_bytes:
            raise ProtocolLimitExceeded("ACP inbound frame exceeded the configured limit")
        self._count_message(len(line))
        if line in {b"\n", b"\r\n"}:
            raise ACPProtocolError("ACP process emitted an empty JSON-RPC frame")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ACPProtocolError("ACP process emitted malformed JSON-RPC data") from exc
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ACPProtocolError("ACP process emitted an invalid JSON-RPC envelope")
        has_method = isinstance(value.get("method"), str)
        has_id = isinstance(value.get("id"), (str, int))
        if not has_method and not has_id:
            raise ACPProtocolError("ACP process emitted an uncorrelated JSON-RPC message")
        return value

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class _SandboxACPClient:
    """Redacted update collector and sandbox-bound permission responder."""

    def __init__(
        self,
        max_updates: int,
        on_progress: Callable[[Mapping[str, int]], Awaitable[None]] | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.max_updates = max_updates
        self.on_progress = on_progress
        self.workspace_root = workspace_root
        self.update_counts: Counter[str] = Counter()
        self.limit_exceeded = asyncio.Event()
        self.progress_error: Exception | None = None

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        paths = [location.path for location in tool_call.locations or []]
        if permission_allowed(tool_call.kind, paths, self.workspace_root):
            allow = next((option for option in options if option.kind == "allow_once"), None)
            if allow is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(option_id=allow.option_id, outcome="selected")
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        kind = str(getattr(update, "session_update", "unknown"))
        self.update_counts[kind] += 1
        count = sum(self.update_counts.values())
        if count > self.max_updates:
            self.limit_exceeded.set()
        elif self.on_progress is not None and count % 100 == 0:
            try:
                await self.on_progress(dict(self.update_counts))
            except Exception as exc:  # noqa: BLE001 - callback failures must stop this run.
                self.progress_error = exc
                self.limit_exceeded.set()
        # Message text, thought content, tool input, and tool output are not retained.


class ACPAgentSession:
    """One ACP process/session. Each attempt starts a new protocol session."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        max_frame_bytes: int,
        max_updates: int,
        idle_timeout_seconds: float = 300,
        on_progress: Callable[[Mapping[str, int]], Awaitable[None]] | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ACPProtocolError("ACP subprocess requires stdin and stdout pipes")
        self.process = process
        self.client = _SandboxACPClient(max_updates, on_progress, workspace_root)
        self.transport = BoundedStdioTransport(
            process.stdout,
            process.stdin,
            max_frame_bytes=max_frame_bytes,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        self.connection = connect_to_agent(
            cast(Client, self.client),
            self.transport,
            receive_timeout=idle_timeout_seconds,
        )
        self.initialize_response: InitializeResponse | None = None
        self.session_id: str | None = None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self.stderr_bytes = 0

    async def initialize(self) -> InitializeResponse:
        try:
            response = await self.connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                    terminal=False,
                ),
                client_info=Implementation(name="harnessa", version="0.1.0"),
            )
        except Exception as exc:
            raise ACPProtocolError("ACP agent failed during initialization") from exc
        if response.protocol_version != PROTOCOL_VERSION:
            raise ACPProtocolError("ACP agent negotiated an unsupported protocol version")
        self.initialize_response = response
        return response

    async def new_session(self, *, mcp_servers: list[McpServerStdio] | None = None) -> str:
        configured_servers = cast(
            list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio],
            mcp_servers or [],
        )
        try:
            created = await self.connection.new_session(
                cwd="/workspace", mcp_servers=configured_servers
            )
        except Exception as exc:
            raise ACPProtocolError("ACP agent failed to create a session") from exc
        session_id = created.session_id
        self.session_id = session_id
        return session_id

    async def prompt(self, content: str, *, timeout_seconds: float) -> str:
        if self.session_id is None:
            raise ACPProtocolError("ACP session was not created")
        prompt_task = asyncio.create_task(
            self.connection.prompt(self.session_id, [text_block(content)])
        )
        limit_task = asyncio.create_task(self.client.limit_exceeded.wait())
        try:
            done, _ = await asyncio.wait(
                {prompt_task, limit_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("ACP prompt exceeded the configured run deadline")
            if limit_task in done and limit_task.result():
                if self.client.progress_error is not None:
                    raise self.client.progress_error
                raise ProtocolLimitExceeded("ACP agent exceeded the session update limit")
            try:
                response = await prompt_task
            except Exception as exc:
                raise ACPProtocolError("ACP agent failed while handling a prompt") from exc
            return str(response.stop_reason)
        finally:
            for task in (prompt_task, limit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prompt_task, limit_task, return_exceptions=True)

    async def cancel(self) -> None:
        if self.session_id is not None and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.connection.cancel(self.session_id), timeout=2)
            except Exception:  # noqa: BLE001 - process shutdown remains the cancellation fallback.
                logger.warning("ACP protocol cancellation failed; terminating the agent process")

    async def close(self, *, grace_seconds: float = 2) -> None:
        try:
            await self.connection.close()
        finally:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await self._stderr_task

    async def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                return
            self.stderr_bytes += len(chunk)
