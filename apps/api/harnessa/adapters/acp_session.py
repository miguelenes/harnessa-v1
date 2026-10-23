"""Bounded ACP stdio session built on the official Python SDK client connection."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.interfaces import Client
from acp.schema import (
    AcpMcpServer,
    AgentMessageChunk,
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
    TextContent,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

from harnessa.adapters.sandbox_policy import permission_allowed
from harnessa.domain.content_safety import safe_content_for_ui

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
        on_ui_event: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.max_updates = max_updates
        self.on_progress = on_progress
        self.on_ui_event = on_ui_event
        self.workspace_root = workspace_root
        self.update_counts: Counter[str] = Counter()
        self.limit_exceeded = asyncio.Event()
        self.progress_error: Exception | None = None
        self._max_ui_events = min(max_updates, 512)
        self._ui_event_count = 0
        self._ui_reservations: dict[str, int] = {}
        self._ui_reserved_events = 0
        self._step_started = False
        self._message_source_id: str | None = None
        self._message_id: str | None = None
        self._message_chunks: list[str] = []
        self._message_char_count = 0
        self._message_overflow = False
        self._message_count = 0
        self._tool_ids: dict[str, str] = {}

    async def _emit_ui_event(self, event: Mapping[str, Any]) -> bool:
        if self.on_ui_event is None:
            return True
        kind = event.get("kind")
        reservation_key: str | None = None
        reserve = 0
        release = 0
        if kind == "step_started":
            reservation_key, reserve = "step", 1
        elif kind == "message_started" and isinstance(event.get("message_id"), str):
            reservation_key, reserve = f"message:{event['message_id']}", 2
        elif kind == "tool_started" and isinstance(event.get("tool_call_id"), str):
            reservation_key, reserve = f"tool:{event['tool_call_id']}", 2
        elif kind in {"message_content", "message_ended"} and isinstance(
            event.get("message_id"), str
        ):
            reservation_key = f"message:{event['message_id']}"
            release = 1
        elif kind == "step_finished":
            reservation_key, release = "step", 1
        elif kind in {"tool_ended", "tool_result_redacted"} and isinstance(
            event.get("tool_call_id"), str
        ):
            reservation_key = f"tool:{event['tool_call_id']}"
            release = 1
        current_reservation = self._ui_reservations.get(reservation_key or "", 0)
        released = min(release, current_reservation)
        reserved_after = self._ui_reserved_events + reserve - released
        if self._ui_event_count + 1 + reserved_after > self._max_ui_events:
            self.progress_error = self.progress_error or ProtocolLimitExceeded(
                "ACP agent exhausted the bounded UI event buffer"
            )
            self.limit_exceeded.set()
            return False
        try:
            await self.on_ui_event(event)
            self._ui_event_count += 1
            self._ui_reserved_events = reserved_after
            if reservation_key is not None:
                next_reservation = current_reservation + reserve - released
                if next_reservation:
                    self._ui_reservations[reservation_key] = next_reservation
                else:
                    self._ui_reservations.pop(reservation_key, None)
            return True
        except Exception as exc:  # noqa: BLE001 - durable event failure stops unsafe continuation.
            self.progress_error = self.progress_error or exc
            self.limit_exceeded.set()
            return False

    def _release_ui_reservation(self, key: str, count: int) -> None:
        current = self._ui_reservations.get(key, 0)
        released = min(count, current)
        if not released:
            return
        self._ui_reserved_events -= released
        remaining = current - released
        if remaining:
            self._ui_reservations[key] = remaining
        else:
            self._ui_reservations.pop(key, None)

    async def start_step(self) -> None:
        self._step_started = await self._emit_ui_event(
            {"kind": "step_started", "step_name": "agent_execution"}
        )

    async def finish_step(self, outcome: str) -> None:
        for source_id, tool_id in tuple(self._tool_ids.items()):
            await self._emit_ui_event({"kind": "tool_ended", "tool_call_id": tool_id})
            await self._emit_ui_event(
                {"kind": "tool_result_redacted", "tool_call_id": tool_id}
            )
            self._tool_ids.pop(source_id, None)
        # Cancellation closes the message envelope without publishing a partial
        # assistant response after the user asked the run to stop.
        await self.finish_message(include_content=outcome != "cancelled")
        if self._step_started:
            await self._emit_ui_event(
                {"kind": "step_finished", "step_name": "agent_execution", "outcome": outcome}
            )
            self._step_started = False

    async def finish_message(self, *, include_content: bool = True) -> None:
        if self._message_id is None:
            return
        message_id = self._message_id
        self._message_count += 1
        if include_content:
            if self._message_overflow:
                text = "Agent message omitted because it exceeded the safe display limit."
            else:
                text = "".join(self._message_chunks)
                if not safe_content_for_ui(text):
                    text = "Agent message omitted by Harnessa's credential safety policy."
            await self._emit_ui_event(
                {"kind": "message_content", "message_id": message_id, "text": text}
            )
        else:
            self._release_ui_reservation(f"message:{message_id}", 1)
        await self._emit_ui_event({"kind": "message_ended", "message_id": message_id})
        self._message_source_id = None
        self._message_id = None
        self._message_chunks.clear()
        self._message_char_count = 0
        self._message_overflow = False

    async def _tool_update(self, update: ToolCallStart | ToolCallProgress | ToolCallUpdate) -> None:
        source_id = getattr(update, "tool_call_id", "")
        if not isinstance(source_id, str) or not source_id:
            return
        await self.finish_message()
        tool_id = self._tool_ids.get(source_id)
        if isinstance(update, ToolCallStart) and tool_id is None:
            tool_id = str(uuid4())
            kind = getattr(update, "kind", None)
            safe_kind = kind if kind in {
                "read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"
            } else "other"
            started = await self._emit_ui_event(
                {"kind": "tool_started", "tool_call_id": tool_id, "tool_name": safe_kind}
            )
            if started:
                self._tool_ids[source_id] = tool_id
                await self._emit_ui_event(
                    {"kind": "tool_args_redacted", "tool_call_id": tool_id}
                )
        status = getattr(update, "status", None)
        if tool_id and status in {"completed", "failed"}:
            await self._emit_ui_event(
                {"kind": "tool_ended", "tool_call_id": tool_id, "outcome": status}
            )
            await self._emit_ui_event(
                {"kind": "tool_result_redacted", "tool_call_id": tool_id}
            )
            self._tool_ids.pop(source_id, None)

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
        elif isinstance(update, AgentMessageChunk):
            content = update.content
            if not isinstance(content, TextContent):
                await self.finish_message()
                return
            source_id = update.message_id or f"anonymous-{self._message_count + 1}"
            if source_id != self._message_source_id:
                await self.finish_message()
                self._message_source_id = source_id
                message_id = str(uuid4())
                if await self._emit_ui_event(
                    {"kind": "message_started", "message_id": message_id}
                ):
                    self._message_id = message_id
                else:
                    self._message_source_id = None
                    return
            chunk = content.text
            if self._message_char_count + len(chunk) > 8192:
                self._message_overflow = True
                self._message_chunks.clear()
            elif not self._message_overflow:
                self._message_chunks.append(chunk)
            self._message_char_count += len(chunk)
        elif isinstance(update, (ToolCallStart, ToolCallProgress, ToolCallUpdate)):
            await self._tool_update(update)
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
        on_ui_event: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ACPProtocolError("ACP subprocess requires stdin and stdout pipes")
        self.process = process
        self.client = _SandboxACPClient(
            max_updates, on_progress, on_ui_event, workspace_root
        )
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
        self._stderr_tail = bytearray()

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
        await self.client.start_step()
        prompt_task = asyncio.create_task(
            self.connection.prompt(self.session_id, [text_block(content)])
        )
        limit_task = asyncio.create_task(self.client.limit_exceeded.wait())
        outcome = "failed"
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
                exc.__dict__["harnessa_diagnostics"] = self._stderr_categories()
                raise ACPProtocolError("ACP agent failed while handling a prompt") from exc
            outcome = "cancelled" if response.stop_reason == "cancelled" else "completed"
            return str(response.stop_reason)
        finally:
            for task in (prompt_task, limit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prompt_task, limit_task, return_exceptions=True)
            await self.client.finish_step(outcome)
            if self.client.progress_error is not None:
                raise self.client.progress_error

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
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > 65_536:
                del self._stderr_tail[:-65_536]

    def _stderr_categories(self) -> list[str]:
        """Classify a bounded stderr tail without retaining or returning log text."""
        text = self._stderr_tail.decode("utf-8", errors="ignore").lower()
        rules = {
            "connection_refused": r"econnrefused|connection refused",
            "connection_reset": r"econnreset|connection reset",
            "dns_failure": r"enotfound|name resolution failed",
            "timeout": r"etimedout|timed out|deadline exceeded",
            "authentication_rejected": r"401 unauthorized|invalid api key|authentication failed",
            "permission_rejected": r"403 forbidden|permission denied",
            "budget_rejected": r"402 payment required|insufficient budget|reservation limit",
            "rate_limited": r"429 too many requests|rate limit|quota exceeded",
            "provider_unavailable": r"502 bad gateway|503 service unavailable|model not found",
        }
        return [category for category, pattern in rules.items() if re.search(pattern, text)]
