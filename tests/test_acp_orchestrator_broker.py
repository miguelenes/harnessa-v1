from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harnessa.adapters.acp_orchestrator_broker import (
    ACPOrchestratorBroker,
    OrchestratorScope,
)


@pytest.mark.anyio
async def test_clarification_is_persisted_before_wait_and_returns_scoped_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harnessa.adapters.acp_orchestrator_broker as module

    scope = OrchestratorScope(uuid4(), uuid4(), uuid4(), 2, 7)
    question_id = uuid4()
    order: list[str] = []

    @asynccontextmanager
    async def sessions():
        yield object()

    async def create(db: object, **kwargs: object) -> object:
        assert db is not None
        assert kwargs == {
            "interview_id": scope.interview_id,
            "task_id": scope.task_id,
            "run_id": scope.run_id,
            "attempt": 2,
            "fencing_token": 7,
            "question_text": "Which supported output format should this change use?",
        }
        order.append("persist")
        return SimpleNamespace(id=question_id)

    async def wait_for_answer(**kwargs: object) -> tuple[str, bool]:
        assert kwargs["question_id"] == question_id
        assert kwargs["task_id"] == scope.task_id
        assert kwargs["run_id"] == scope.run_id
        assert kwargs["fencing_token"] == scope.fencing_token
        order.append("wait")
        return "Use JSON.", False

    monkeypatch.setattr(module, "create_agent_question", create)
    monkeypatch.setattr(module, "wait_for_agent_answer", wait_for_answer)
    broker = ACPOrchestratorBroker(
        socket_path=tmp_path / "broker" / "clarification.sock",
        scope=scope,
        sessions=sessions,
        is_cancelled=lambda: _result(False),
    )
    await broker.start()
    try:
        assert broker.socket_path.stat().st_mode & 0o777 == 0o600
        reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
        writer.write(
            json.dumps({"question": "Which supported output format should this change use?"}).encode()
            + b"\n"
        )
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert order == ["persist", "wait"]
        assert response == {
            "ok": True,
            "answer": "Use JSON.",
            "scope_change": False,
            "question_id": str(question_id),
        }
    finally:
        await broker.close()
    assert not broker.socket_path.exists()


@pytest.mark.anyio
async def test_broker_rejects_invalid_and_cancelled_questions_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harnessa.adapters.acp_orchestrator_broker as module

    async def must_not_create(*_: object, **__: object) -> object:
        raise AssertionError("invalid or cancelled request must not be persisted")

    monkeypatch.setattr(module, "create_agent_question", must_not_create)
    cancelled = False
    broker = ACPOrchestratorBroker(
        socket_path=tmp_path / "broker.sock",
        scope=OrchestratorScope(uuid4(), uuid4(), uuid4(), 1, 1),
        sessions=lambda: None,  # type: ignore[arg-type]
        is_cancelled=lambda: _result(cancelled),
    )
    await broker.start()
    try:
        for body, error in (
            (b"not json\n", "Clarification is unavailable; stop and ask the operator."),
            (b'{"question":"short"}\n', "Clarification is unavailable; stop and ask the operator."),
            (b'{"question":"A sufficiently long question?","extra":true}\n', "Clarification is unavailable; stop and ask the operator."),
        ):
            response = await _request(broker.socket_path, body)
            assert response == {"ok": False, "error": error}

        cancelled = True
        response = await _request(
            broker.socket_path, b'{"question":"Why is this requirement needed?"}\n'
        )
        assert response == {
            "ok": False,
            "error": "Clarification is unavailable; stop and ask the operator.",
        }
    finally:
        await broker.close()


async def _request(path: Path, body: bytes) -> object:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(body)
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


async def _result(value: bool) -> bool:
    return value
