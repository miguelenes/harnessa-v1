from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from harnessa.adapters.acp_model_broker import ACPModelBroker, BrokerScope
from harnessa.contracts import ModelRouteRequest


class _Session(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _Router:
    def __init__(self) -> None:
        self.requests: list[ModelRouteRequest] = []
        self.scope: tuple[UUID, UUID] | None = None

    async def complete(self, db: Any, *, organization_id, project_id, request, idempotency_key):
        del db, idempotency_key
        self.scope = organization_id, project_id
        self.requests.append(request)
        return {"response": {"id": "offline", "choices": [{"message": {"content": "ok"}}]}}

    async def stream(self, db: Any, *, organization_id, project_id, request, idempotency_key):
        del db, organization_id, project_id, idempotency_key
        self.requests.append(request)

        async def chunks():
            yield b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\n"
            yield b"data: [DONE]\n\n"

        return uuid4(), chunks()


async def _client_for(
    tmp_path: Path,
    *,
    router: _Router,
    current_fence: bool = True,
    cancelled: bool = False,
) -> tuple[httpx.AsyncClient, ACPModelBroker]:
    task_id, run_id = uuid4(), uuid4()
    broker = ACPModelBroker(
        socket_path=tmp_path / "model.sock",
        scope=BrokerScope(
            token="scoped-token",
            organization_id=uuid4(),
            project_id=uuid4(),
            task_id=task_id,
            run_id=run_id,
            fencing_token=7,
            model_alias="budgeted-code",
        ),
        model_router=router,
        sessions=_Session,
        is_current_fence=lambda requested_run, token: _resolved(
            requested_run == run_id and token == 7 and current_fence
        ),
        is_cancelled=lambda: _resolved(cancelled),
    )
    await broker.start()
    transport = httpx.AsyncHTTPTransport(uds=str(broker.socket_path))
    return httpx.AsyncClient(transport=transport, base_url="http://broker"), broker


async def _resolved(value: bool) -> bool:
    return value


@pytest.mark.anyio
async def test_broker_binds_model_calls_to_run_scope_and_forwards_tools(tmp_path: Path) -> None:
    router = _Router()
    client, broker = await _client_for(tmp_path, router=router)
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer scoped-token"},
            json={
                "model": "agent-selected-unbudgeted-model",
                "max_tokens": 128,
                "messages": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call-1", "type": "function", "function": {"name": "edit"}}
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "changed"},
                ],
                "tools": [{"type": "function", "function": {"name": "edit"}}],
            },
        )
        assert response.status_code == 200
        assert router.scope is not None
        request = router.requests[0]
        assert request.task_id is not None and request.run_id is not None
        assert request.model_alias == "budgeted-code"
        assert request.max_output_tokens == 128
        assert request.requirements.tools
        assert request.messages is not None
        assert request.messages[0].tool_calls is not None
        assert request.messages[1].tool_call_id == "call-1"
        assert response.json()["choices"][0]["message"]["content"] == "ok"
    finally:
        await client.aclose()
        await broker.close()


@pytest.mark.anyio
async def test_broker_rejects_missing_credentials_stale_fences_and_unbounded_output(
    tmp_path: Path,
) -> None:
    router = _Router()
    client, broker = await _client_for(tmp_path, router=router, current_fence=False)
    request = {"max_tokens": 128, "messages": [{"role": "user", "content": "hello"}]}
    try:
        assert (await client.post("/v1/chat/completions", json=request)).status_code == 401
        stale = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer scoped-token"},
            json=request,
        )
        assert stale.status_code == 409
        assert router.requests == []
    finally:
        await client.aclose()
        await broker.close()

    router = _Router()
    client, broker = await _client_for(tmp_path, router=router)
    try:
        oversized = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer scoped-token"},
            json={"max_tokens": 70_000, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert oversized.status_code == 400
        assert router.requests == []
    finally:
        await client.aclose()
        await broker.close()


@pytest.mark.anyio
async def test_broker_streams_only_through_router_and_observes_run_cancel(tmp_path: Path) -> None:
    router = _Router()
    client, broker = await _client_for(tmp_path, router=router)
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer scoped-token"},
            json={
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert "[DONE]" in response.text
        assert router.requests[0].stream
    finally:
        await client.aclose()
        await broker.close()
