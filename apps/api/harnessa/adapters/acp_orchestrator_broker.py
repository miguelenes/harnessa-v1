"""Run-scoped MCP clarification bridge backed by Harnessa's durable question records."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.db.session import SessionLocal
from harnessa.services.specifications import create_agent_question, wait_for_agent_answer

logger = logging.getLogger("harnessa.acp_orchestrator_broker")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class OrchestratorScope:
    """Identity captured from the scheduler; the sandbox cannot choose these IDs."""

    interview_id: UUID
    task_id: UUID
    run_id: UUID
    attempt: int
    fencing_token: int


class ACPOrchestratorBroker:
    """Persist a clarification before waiting for its authorized answer."""

    def __init__(
        self,
        *,
        socket_path: Path,
        scope: OrchestratorScope,
        sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
        is_cancelled: Callable[[], Awaitable[bool]],
    ) -> None:
        self.socket_path = socket_path
        self.scope = scope
        self.sessions = sessions
        self.is_cancelled = is_cancelled
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=_MAX_REQUEST_BYTES + 1
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
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if not raw or len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ValueError("invalid or oversized request")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"question"}:
                raise ValueError("request must contain only a question")
            question_text = payload.get("question")
            if not isinstance(question_text, str) or not 12 <= len(question_text.strip()) <= 2000:
                raise ValueError("question length is outside the allowed bounds")
            if await self.is_cancelled():
                raise RuntimeError("run is no longer active")
            async with self.sessions() as db:
                question = await create_agent_question(
                    db,
                    interview_id=self.scope.interview_id,
                    task_id=self.scope.task_id,
                    run_id=self.scope.run_id,
                    attempt=self.scope.attempt,
                    fencing_token=self.scope.fencing_token,
                    question_text=question_text,
                )
                question_id = question.id
            answer, scope_change = await wait_for_agent_answer(
                question_id=question_id,
                task_id=self.scope.task_id,
                run_id=self.scope.run_id,
                fencing_token=self.scope.fencing_token,
                sessions=self.sessions,
            )
            response = {
                "ok": True,
                "answer": answer,
                "scope_change": scope_change,
                "question_id": str(question_id),
            }
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - sandbox receives only a bounded safe error.
            logger.info("ACP clarification bridge rejected a request: %s", type(exc).__name__)
            response = {"ok": False, "error": "Clarification is unavailable; stop and ask the operator."}
        try:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_RESPONSE_BYTES:
                encoded = b'{"ok":false,"error":"Clarification response exceeded its size limit."}'
            writer.write(encoded + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
