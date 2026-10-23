"""Run-scoped OpenAI-compatible bridge into Harnessa's budgeted model router."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.adapters.protocols import ModelRouter
from harnessa.contracts import (
    ModelMessage,
    ModelPoolKind,
    ModelProtocol,
    ModelRouteRequest,
    RouteRequirements,
)
from harnessa.services.model_gateway import ModelGatewayError

logger = logging.getLogger("harnessa.acp_model_broker")
_MAX_HEADERS = 16 * 1024
_MAX_BODY = 1024 * 1024
_DEFAULT_MAX_BODY = 512 * 1024
_MAX_OUTPUT_TOKENS = 65_536
_MAX_MESSAGES = 2_000
_MAX_TOOLS = 256


@dataclass(frozen=True)
class BrokerScope:
    token: str
    organization_id: UUID
    project_id: UUID
    task_id: UUID
    run_id: UUID
    fencing_token: int
    model_alias: str | None = None
    model_pool: ModelPoolKind = ModelPoolKind.INTERACTIVE


class ACPModelBroker:
    """Expose one run's model route over a private Unix socket, never provider keys."""

    def __init__(
        self,
        *,
        socket_path: Path,
        scope: BrokerScope,
        model_router: ModelRouter,
        sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        is_current_fence: Callable[[UUID, int], Awaitable[bool]],
        is_cancelled: Callable[[], Awaitable[bool]],
        max_output_tokens: int = 8192,
        max_request_bytes: int = _DEFAULT_MAX_BODY,
    ) -> None:
        self.socket_path = socket_path
        self.scope = scope
        self.model_router = model_router
        self.sessions = sessions
        self.is_current_fence = is_current_fence
        self.is_cancelled = is_cancelled
        self.max_output_tokens = min(max_output_tokens, _MAX_OUTPUT_TOKENS)
        self.max_request_bytes = min(max_request_bytes, _MAX_BODY)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=_MAX_HEADERS + 1
        )
        self.socket_path.chmod(0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response_task: asyncio.Task[None] | None = None
        monitor_task: asyncio.Task[None] | None = None
        try:
            method, path, headers, body = await _read_request(
                reader, max_body_bytes=self.max_request_bytes
            )
            if method != "POST" or path != "/v1/chat/completions":
                await _send_json(writer, 404, {"error": {"message": "Route not found"}})
                return
            if not _authorized(headers.get("authorization"), self.scope.token):
                await _send_json(writer, 401, {"error": {"message": "Unauthorized"}})
                return
            if not await self.is_current_fence(self.scope.run_id, self.scope.fencing_token):
                await _send_json(writer, 409, {"error": {"message": "Run lease is no longer current"}})
                return
            if await self.is_cancelled():
                await _send_json(writer, 409, {"error": {"message": "Run is cancelled"}})
                return
            request = _route_request(body, self.scope, self.max_output_tokens)
            response_task = asyncio.create_task(self._route(writer, request, headers))
            monitor_task = asyncio.create_task(self._watch_run())
            done, _ = await asyncio.wait(
                {response_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor_task in done:
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            else:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
                await response_task
        except (ValueError, ValidationError) as exc:
            await _send_json(writer, 400, {"error": {"message": str(exc)[:300]}})
        except ModelGatewayError as exc:
            await _send_json(
                writer,
                exc.status_code if 400 <= exc.status_code <= 599 else 503,
                {"error": {"message": str(exc)[:300], "code": exc.code}},
            )
        except asyncio.CancelledError:
            if response_task is not None:
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            raise
        except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError):
            if response_task is not None:
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
        except Exception:
            logger.exception("ACP model route failed for run %s", self.scope.run_id)
            if not writer.is_closing():
                await _send_json(writer, 503, {"error": {"message": "Model route unavailable"}})
        finally:
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def _watch_run(self) -> None:
        while True:
            if await self.is_cancelled() or not await self.is_current_fence(
                self.scope.run_id, self.scope.fencing_token
            ):
                return
            await asyncio.sleep(0.5)

    async def _route(
        self,
        writer: asyncio.StreamWriter,
        request: ModelRouteRequest,
        headers: dict[str, str],
    ) -> None:
        async with self.sessions() as db:
            if request.stream:
                request_id, chunks = await self.model_router.stream(
                    db,
                    organization_id=self.scope.organization_id,
                    project_id=self.scope.project_id,
                    request=request,
                    idempotency_key=_idempotency_key(headers),
                )
                del request_id
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                )
                async for chunk in chunks:
                    if await self.is_cancelled() or not await self.is_current_fence(
                        self.scope.run_id, self.scope.fencing_token
                    ):
                        raise asyncio.CancelledError
                    writer.write(chunk)
                    await writer.drain()
                return

            result = await self.model_router.complete(
                db,
                organization_id=self.scope.organization_id,
                project_id=self.scope.project_id,
                request=request,
                idempotency_key=_idempotency_key(headers),
            )
            if await self.is_cancelled() or not await self.is_current_fence(
                self.scope.run_id, self.scope.fencing_token
            ):
                raise asyncio.CancelledError
            response = result.get("response")
            if not isinstance(response, dict):
                raise ModelGatewayError("invalid_model_response", 502, "Model response was invalid")
            await _send_json(writer, 200, response)


async def _read_request(
    reader: asyncio.StreamReader, *, max_body_bytes: int = _MAX_BODY
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    try:
        header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
        raise ValueError("Invalid HTTP request headers") from exc
    if len(header_bytes) > _MAX_HEADERS:
        raise ValueError("HTTP request headers exceed the size limit")
    lines = header_bytes[:-4].decode("latin-1").split("\r\n")
    request_line = lines.pop(0).split()
    if len(request_line) != 3 or request_line[2] not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ValueError("Invalid HTTP request line")
    headers: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError("Invalid HTTP header")
        normalized = key.strip().lower()
        if normalized in headers:
            raise ValueError("Duplicate HTTP header")
        headers[normalized] = value.strip()
    if "transfer-encoding" in headers:
        raise ValueError("Chunked request bodies are not accepted")
    try:
        content_length = int(headers.get("content-length", "-1"))
    except ValueError as exc:
        raise ValueError("Invalid Content-Length") from exc
    if not 0 <= content_length <= max_body_bytes:
        raise ValueError("Request body exceeds the size limit")
    raw = await asyncio.wait_for(reader.readexactly(content_length), timeout=10)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Request body must be an object")
    return request_line[0].upper(), request_line[1], headers, value


def _route_request(
    body: dict[str, Any], scope: BrokerScope, max_output_tokens: int
) -> ModelRouteRequest:
    messages_raw = body.get("messages")
    if not isinstance(messages_raw, list) or not 1 <= len(messages_raw) <= _MAX_MESSAGES:
        raise ValueError("Request must contain a bounded messages array")
    tools = body.get("tools")
    if tools is not None and (not isinstance(tools, list) or len(tools) > _MAX_TOOLS):
        raise ValueError("Request tools exceed the supported limit")
    requested_output = body.get("max_tokens", body.get("max_completion_tokens"))
    if isinstance(requested_output, bool) or not isinstance(requested_output, int):
        raise TypeError("A bounded max_tokens value is required")
    if not 1 <= requested_output <= max_output_tokens:
        raise ValueError("Requested output exceeds the run-scoped limit")
    serialized = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    # One token per request byte is conservative and cannot under-reserve usage.
    estimated_input = max(1, len(serialized))
    if estimated_input > 2_000_000:
        raise ValueError("Estimated input exceeds the supported token limit")
    stream = body.get("stream") is True
    parsed_messages: list[ModelMessage] = []
    for message in messages_raw:
        if not isinstance(message, dict):
            raise TypeError("Each message must be an object")
        parsed_messages.append(ModelMessage.model_validate(message))
    return ModelRouteRequest(
        task_id=scope.task_id,
        run_id=scope.run_id,
        pool=scope.model_pool,
        model_alias=scope.model_alias,
        requirements=RouteRequirements(
            protocol=ModelProtocol.CHAT_COMPLETIONS,
            streaming=stream,
            tools=bool(tools),
            minimum_context=min(2_000_000, estimated_input + requested_output),
            usage_reporting=True,
            cancellation=True,
        ),
        max_output_tokens=requested_output,
        estimated_input_tokens=estimated_input,
        stream=stream,
        messages=parsed_messages,
        tools=tools,
    )


def _authorized(value: str | None, expected: str) -> bool:
    if value is None:
        return False
    scheme, separator, token = value.partition(" ")
    return bool(separator and scheme.lower() == "bearer" and hmac.compare_digest(token, expected))


def _idempotency_key(headers: dict[str, str]) -> str:
    supplied = headers.get("idempotency-key") or headers.get("x-request-id")
    if supplied and len(supplied) <= 200:
        return supplied
    return f"acp-{uuid4()}"


async def _send_json(
    writer: asyncio.StreamWriter, status: int, value: dict[str, Any]
) -> None:
    body = json.dumps(value, separators=(",", ":")).encode()
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Error")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
