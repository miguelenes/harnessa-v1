from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from harnessa.contracts import (
    ModelMessage,
    ModelPoolKind,
    ModelProtocol,
    ModelRouteRequest,
    RouteRequirements,
)
from harnessa.db.model_gateway import (
    AliasDeployment,
    ModelAlias,
    ModelBudgetCounter,
    ModelBudgetPolicy,
    ModelDeployment,
    ModelPool,
    ModelRequestLedger,
    ModelReservation,
    PoolAlias,
    ProviderAccount,
)
from harnessa.db.models import Membership, Organization, Project, ProjectMembership, Task, User
from harnessa.db.orchestration import Run
from harnessa.db.session import SessionLocal
from harnessa.services.model_gateway import (
    LiteLLMModelRouter,
    ModelGatewayError,
    reconcile_stale_model_reservations,
)
from pwdlib import PasswordHash
from sqlalchemy import select, text

pytestmark = pytest.mark.integration


async def gateway_fixture(
    *,
    project_limit: Decimal = Decimal(10),
    task_limit: Decimal = Decimal(10),
    run_limit: Decimal = Decimal(10),
    overflow: int = 0,
):
    suffix = uuid4().hex[:10]
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        org = Organization(name=f"Model Org {suffix}")
        user = User(
            email=f"{suffix}@example.test",
            hashed_password=PasswordHash.recommended().hash("integration-test-password"),
            is_active=True,
            is_superuser=True,
            is_verified=True,
        )
        db.add_all([org, user])
        await db.flush()
        project = Project(organization_id=org.id, name="Model Project", slug=f"model-{suffix}")
        db.add(project)
        await db.flush()
        db.add_all([
            Membership(organization_id=org.id, user_id=user.id, role="administrator"),
            ProjectMembership(project_id=project.id, user_id=user.id, role="administrator"),
        ])
        task = Task(project_id=project.id, title="Model test", description="", stage="IN_PROGRESS", run_status="RUNNING")
        db.add(task)
        await db.flush()
        run = Run(
            task_id=task.id,
            project_id=project.id,
            attempt=1,
            status="RUNNING",
            fencing_token=1,
            started_at=now,
            adapter_name="test-fixture",
        )
        db.add(run)
        interactive_pool = ModelPool(
            organization_id=org.id,
            name="primary",
            kind="interactive",
            max_concurrency=4,
            interactive_reserve=2,
        )
        probe_pool = ModelPool(
            organization_id=org.id,
            name="batch",
            kind="probe",
            max_concurrency=4,
            interactive_reserve=2,
            probe_overflow_limit=overflow,
        )
        primary_account = ProviderAccount(
            organization_id=org.id,
            name="primary-account",
            provider="openai",
            secret_ref="env:OPENAI_API_KEY",
            enabled=True,
        )
        fallback_account = ProviderAccount(
            organization_id=org.id,
            name="fallback-account",
            provider="anthropic",
            secret_ref="env:ANTHROPIC_API_KEY",
            enabled=True,
        )
        alias = ModelAlias(organization_id=org.id, name="coding", description="", enabled=True)
        db.add_all([interactive_pool, probe_pool, primary_account, fallback_account, alias])
        await db.flush()
        capabilities = {
            "protocols": [ModelProtocol.CHAT_COMPLETIONS.value],
            "streaming": True,
            "tools": True,
            "usage_reporting": True,
            "cancellation": False,
        }
        primary = ModelDeployment(
            organization_id=org.id,
            provider_account_id=primary_account.id,
            name="primary-deployment",
            actual_model="provider/primary-model",
            proxy_model="gateway-primary",
            capabilities=capabilities,
            context_window=128_000,
            input_cost_per_million=Decimal(1),
            output_cost_per_million=Decimal(2),
            price_revision="fixture-1",
            max_concurrency=4,
            enabled=True,
        )
        fallback = ModelDeployment(
            organization_id=org.id,
            provider_account_id=fallback_account.id,
            name="fallback-deployment",
            actual_model="provider/fallback-model",
            proxy_model="gateway-fallback",
            capabilities=capabilities,
            context_window=128_000,
            input_cost_per_million=Decimal(1),
            output_cost_per_million=Decimal(2),
            price_revision="fixture-2",
            max_concurrency=4,
            enabled=True,
        )
        db.add_all([primary, fallback])
        await db.flush()
        db.add_all([
            AliasDeployment(alias_id=alias.id, deployment_id=primary.id, priority=0),
            AliasDeployment(alias_id=alias.id, deployment_id=fallback.id, priority=1),
            PoolAlias(pool_id=interactive_pool.id, alias_id=alias.id, priority=0),
            PoolAlias(pool_id=probe_pool.id, alias_id=alias.id, priority=0),
            ModelBudgetPolicy(
                organization_id=org.id,
                project_id=project.id,
                project_limit_usd=project_limit,
                task_limit_usd=task_limit,
                run_limit_usd=run_limit,
                project_token_limit=1_000_000,
                task_token_limit=1_000_000,
                run_token_limit=1_000_000,
                max_steps=100,
                max_wall_seconds=3600,
                max_output_tokens=10_000,
                max_attempts=3,
            ),
        ])
        await db.commit()
        return {
            "organization_id": org.id,
            "user_id": user.id,
            "project_id": project.id,
            "task_id": task.id,
            "run_id": run.id,
            "primary_account_id": primary_account.id,
            "fallback_account_id": fallback_account.id,
            "primary_deployment_id": primary.id,
            "fallback_deployment_id": fallback.id,
            "interactive_pool_id": interactive_pool.id,
        }


def model_request(data, *, pool=ModelPoolKind.INTERACTIVE, streaming=False):
    return ModelRouteRequest(
        task_id=data["task_id"],
        run_id=data["run_id"],
        pool=pool,
        model_alias="coding",
        requirements=RouteRequirements(
            protocol=ModelProtocol.CHAT_COMPLETIONS,
            streaming=streaming,
            tools=streaming,
            usage_reporting=True,
            minimum_context=64,
        ),
        max_output_tokens=10,
        estimated_input_tokens=10,
        stream=streaming,
        messages=[ModelMessage(role="user", content="fixture request")],
        tools=[{"type": "function", "function": {"name": "safe_fixture"}}] if streaming else None,
    )


def success_response(model: str = "gateway-fallback") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-litellm-call-id": "call-fixture", "x-litellm-response-cost": "0.00001"},
        json={
            "id": "completion-fixture",
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "fixture"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        },
    )


@pytest.mark.anyio
async def test_fault_injected_retries_fallback_and_actual_attribution():
    data = await gateway_fixture()
    seen: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        model = request.read().decode()
        assert "fixture request" in model
        route = json.loads(model)["model"]
        seen.append(route)
        if route == "gateway-primary" and seen.count(route) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "fixture"})
        if route == "gateway-primary":
            return httpx.Response(503, json={"error": "fixture"})
        return success_response(route)

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    try:
        async with SessionLocal() as db:
            result = await LiteLLMModelRouter(client=client).complete(
                db,
                organization_id=data["organization_id"],
                project_id=data["project_id"],
                request=model_request(data),
                idempotency_key="retry-fallback-1",
            )
        assert seen == ["gateway-primary", "gateway-primary", "gateway-fallback"]
        assert result["provider_account"] == "fallback-account"
        assert result["actual_model"] == "provider/fallback-model"
        assert result["usage_known"] is False  # The preceding attempts had unknown billing.
        async with SessionLocal() as db:
            events = (
                await db.execute(
                    select(ModelRequestLedger)
                    .where(ModelRequestLedger.project_id == data["project_id"])
                    .order_by(ModelRequestLedger.created_at, ModelRequestLedger.id)
                )
            ).scalars().all()
            assert [event.event_type for event in events].count("attempt_failed") == 2
            success = next(event for event in events if event.event_type == "attempt_succeeded")
            assert success.account_id == data["fallback_account_id"]
            assert success.deployment_id == data["fallback_deployment_id"]
            assert success.actual_model == "provider/fallback-model"
            assert success.request_identifier == "call-fixture"
            assert all("fixture request" not in json.dumps(event.details) for event in events)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_provider_timeouts_are_bounded_and_fall_back_explicitly():
    data = await gateway_fixture()
    seen: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        route = json.loads(request.content)["model"]
        seen.append(route)
        if route == "gateway-primary":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return success_response(route)

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    try:
        async with SessionLocal() as db:
            result = await LiteLLMModelRouter(client=client).complete(
                db,
                organization_id=data["organization_id"],
                project_id=data["project_id"],
                request=model_request(data),
                idempotency_key="timeout-fallback-1",
            )
        assert seen == ["gateway-primary", "gateway-primary", "gateway-fallback"]
        assert len(seen) <= 3
        assert result["provider_account"] == "fallback-account"
        async with SessionLocal() as db:
            failures = (
                await db.execute(
                    select(ModelRequestLedger).where(
                        ModelRequestLedger.project_id == data["project_id"],
                        ModelRequestLedger.event_type == "attempt_failed",
                    )
                )
            ).scalars().all()
            assert len(failures) == 2
            assert {event.error_code for event in failures} == {"provider_timeout"}
    finally:
        await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("limited_scope", ["project", "task", "run"])
async def test_concurrent_reservations_cannot_exceed_project_task_or_run_limits(limited_scope: str):
    reserve_amount = Decimal("0.00009")  # 20 estimated tokens * $3 / million * 3 attempts
    data = await gateway_fixture(
        project_limit=reserve_amount if limited_scope == "project" else Decimal(10),
        task_limit=reserve_amount if limited_scope == "task" else Decimal(10),
        run_limit=reserve_amount if limited_scope == "run" else Decimal(10),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return success_response("gateway-primary")

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    router = LiteLLMModelRouter(client=client)
    first = asyncio.create_task(_complete(router, data, "budget-request-1"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(ModelGatewayError) as raised:
            await _complete(router, data, "budget-request-2")
        assert raised.value.code == "model_budget_exceeded"
        release.set()
        result = await first
        assert result["actual_model"] == "provider/primary-model"
        async with SessionLocal() as db:
            counters = (
                await db.execute(
                    select(ModelBudgetCounter).where(ModelBudgetCounter.project_id == data["project_id"])
                )
            ).scalars().all()
            assert {counter.scope for counter in counters} == {"project", "task", "run"}
            assert all(counter.reserved_usd == 0 for counter in counters)
            assert all(counter.used_steps <= 1 for counter in counters)
    finally:
        release.set()
        if not first.done():
            await first
        await client.aclose()


async def _complete(router: LiteLLMModelRouter, data, key: str):
    async with SessionLocal() as db:
        return await router.complete(
            db,
            organization_id=data["organization_id"],
            project_id=data["project_id"],
            request=model_request(data),
            idempotency_key=key,
        )


@pytest.mark.anyio
async def test_probe_capacity_preserves_interactive_reserve():
    data = await gateway_fixture()
    entered = 0
    release = asyncio.Event()

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal entered
        entered += 1
        await release.wait()
        return success_response("gateway-primary")

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    router = LiteLLMModelRouter(client=client)
    probes = [
        asyncio.create_task(_complete_request(router, data, f"probe-{index}", ModelPoolKind.PROBE))
        for index in range(2)
    ]
    try:
        await _wait_for_entries(lambda: entered, 2)
        # Third probe is rejected while the two regular batch slots are occupied.
        with pytest.raises(ModelGatewayError) as raised:
            await _complete_request(router, data, "probe-over-capacity", ModelPoolKind.PROBE)
        assert raised.value.code == "probe_capacity_exhausted"
        interactive = asyncio.create_task(
            _complete_request(router, data, "interactive-reserved", ModelPoolKind.INTERACTIVE)
        )
        await _wait_for_entries(lambda: entered, 3)
        release.set()
        await asyncio.gather(*probes, interactive)
    finally:
        release.set()
        await asyncio.gather(*probes, return_exceptions=True)
        await client.aclose()


async def _wait_for_entries(get_count, expected: int) -> None:
    for _ in range(100):
        if get_count() >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"provider received {get_count()} requests, expected {expected}")


async def _complete_request(router, data, key: str, pool: ModelPoolKind):
    async with SessionLocal() as db:
        return await router.complete(
            db,
            organization_id=data["organization_id"],
            project_id=data["project_id"],
            request=model_request(data, pool=pool),
            idempotency_key=key,
        )


class BrokenAfterToolCall(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"safe_fixture"}}]}}]}\n'
        raise httpx.ReadError("fixture stream cut")

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_partial_stream_failure_is_visible_and_never_replays_tools():
    data = await gateway_fixture()
    calls = 0

    async def provider(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=BrokenAfterToolCall())

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    try:
        async with SessionLocal() as db:
            request_id, stream = await LiteLLMModelRouter(client=client).stream(
                db,
                organization_id=data["organization_id"],
                project_id=data["project_id"],
                request=model_request(data, streaming=True),
                idempotency_key="partial-stream-1",
            )
            payload = b"".join([chunk async for chunk in stream]).decode()
        assert calls == 1
        assert "event: model.error" in payload
        assert '"tool_call_chunks_emitted":true' in payload
        async with SessionLocal() as db:
            events = (
                await db.execute(
                    select(ModelRequestLedger).where(ModelRequestLedger.request_id == request_id)
                )
            ).scalars().all()
            failed = next(event for event in events if event.event_type == "attempt_failed")
            assert failed.error_code == "partial_stream_failure"
            assert failed.details["workflow_replayed"] is False
            assert failed.details["tool_call_chunks_emitted"] is True
            assert not any(event.event_type == "fallback_selected" for event in events)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stale_reservation_reconciliation_releases_capacity_and_charges_estimate():
    data = await gateway_fixture()

    async def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline fixture", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="http://litellm")
    # Simulate a process crash after the reservation commit, before result settlement.
    async with SessionLocal() as db:
        from harnessa.services.model_gateway import _reserve, route_candidates

        request = model_request(data)
        pool, candidates = await route_candidates(
            db,
            organization_id=data["organization_id"],
            pool_kind=request.pool.value,
            alias_name=request.model_alias,
            requirements=request.requirements,
        )
        context = await _reserve(
            db,
            organization_id=data["organization_id"],
            project_id=data["project_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            pool=pool,
            candidates=candidates,
            request=request,
            idempotency_key="crashed-process-1",
        )
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE model_reservations SET lease_expires_at = :expired WHERE id = :id"),
            {"expired": datetime.now(UTC).replace(year=2020), "id": context.reservation.id},
        )
        await db.commit()
    reconciled = await reconcile_stale_model_reservations(SessionLocal)
    assert reconciled == 1
    async with SessionLocal() as db:
        reservation = await db.get(ModelReservation, context.reservation.id)
        pool = await db.get(ModelPool, data["interactive_pool_id"])
        counters = (
            await db.execute(select(ModelBudgetCounter).where(ModelBudgetCounter.project_id == data["project_id"]))
        ).scalars().all()
        assert reservation.status == "unknown"
        assert pool.active_interactive == 0 and pool.active_probe == 0
        assert all(counter.reserved_usd == 0 for counter in counters)
        assert all(counter.estimated_usd == context.reservation.reserved_usd for counter in counters)
    await client.aclose()
