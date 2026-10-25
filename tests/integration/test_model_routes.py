from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harnessa.api import model_routes
from harnessa.contracts import (
    ModelAliasCreate,
    ModelBudgetPolicyWrite,
    ModelDeploymentCapabilities,
    ModelDeploymentCreate,
    ModelLiveCheckRequest,
    ModelPoolRouteCreate,
    ModelProtocol,
    ModelProviderAccountCreate,
)
from harnessa.db.model_gateway import (
    ModelBudgetPolicy,
    ModelRequestLedger,
    ProviderAccount,
)
from harnessa.db.models import Membership, Organization, ProjectMembership, User
from harnessa.db.session import SessionLocal
from harnessa.services.model_gateway import ModelGatewayError
from pydantic import SecretStr
from sqlalchemy import select

from tests.integration.test_model_gateway import gateway_fixture, model_request

pytestmark = pytest.mark.integration


async def _admin(data: dict[str, object]) -> User:
    async with SessionLocal() as db:
        user = await db.get(User, data["user_id"])
        assert user is not None
        return user


@pytest.mark.anyio
async def test_model_configuration_routes_preserve_org_boundaries_and_secret_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await gateway_fixture()
    user = await _admin(data)
    monkeypatch.setattr(model_routes.settings, "model_gateway_url", "")

    async with SessionLocal() as db:
        config = await model_routes.get_model_configuration(user, db)
        assert [pool.kind.value for pool in config.pools] == ["interactive", "probe"]
        assert {account.secret_ref for account in config.accounts} == {
            "env:OPENAI_API_KEY",
            "env:ANTHROPIC_API_KEY",
        }
        assert config.aliases[0].deployment_ids

        doctor = await model_routes.model_doctor(user, db)
        status = await model_routes.model_status(user, db)
        assert doctor.status == "setup_required"
        assert doctor.gateway_ready is False
        assert doctor.provider_credentials_checked is False
        assert doctor.live_request_sent is False
        assert status.findings == doctor.findings

        account = await model_routes.create_provider_account(
            ModelProviderAccountCreate(
                name="route-fixture-account",
                provider="openai",
                secret_ref="env:ROUTE_FIXTURE_KEY",
            ),
            user,
            db,
        )
        assert account.enabled is True
        account_id = account.id
        with pytest.raises(HTTPException, match="already exists") as duplicate:
            await model_routes.create_provider_account(
                ModelProviderAccountCreate(
                    name="route-fixture-account",
                    provider="openai",
                    secret_ref="env:ROUTE_FIXTURE_KEY",
                ),
                user,
                db,
            )
        assert duplicate.value.status_code == 409

        capabilities = ModelDeploymentCapabilities(
            protocols=[ModelProtocol.CHAT_COMPLETIONS],
            streaming=True,
            tools=True,
            usage_reporting=True,
        )
        deployment = await model_routes.create_deployment(
            ModelDeploymentCreate(
                name="route-fixture-deployment",
                provider_account_id=account_id,
                actual_model="provider/fixture",
                proxy_model="gateway-route-fixture",
                capabilities=capabilities,
                context_window=4096,
                input_cost_per_million=Decimal("0.2"),
                output_cost_per_million=Decimal("0.4"),
                price_revision="fixture-price-1",
            ),
            user,
            db,
        )
        alias = await model_routes.create_alias(
            ModelAliasCreate(
                name="route-fixture-alias",
                deployment_ids=[deployment.id],
            ),
            user,
            db,
        )
        pool = await db.get(model_routes.ModelPool, data["interactive_pool_id"])
        assert pool is not None
        result = await model_routes.add_pool_route(
            pool.id,
            ModelPoolRouteCreate(model_alias_id=alias.id, priority=1),
            user,
            db,
        )
        assert result == {
            "pool": pool.name,
            "alias": alias.name,
            "priority": 1,
        }

        with pytest.raises(HTTPException) as missing_account:
            await model_routes.create_deployment(
                ModelDeploymentCreate(
                    name="missing-account-deployment",
                    provider_account_id=uuid4(),
                    actual_model="provider/missing",
                    proxy_model="gateway-missing",
                    capabilities=capabilities,
                    context_window=4096,
                    input_cost_per_million=Decimal(0),
                    output_cost_per_million=Decimal(0),
                    price_revision="fixture",
                ),
                user,
                db,
            )
        assert missing_account.value.status_code == 404


@pytest.mark.anyio
async def test_model_budget_usage_and_request_routes_are_project_scoped() -> None:
    data = await gateway_fixture()
    user = await _admin(data)
    async with SessionLocal() as db:
        existing = await db.scalar(
            select(ModelBudgetPolicy).where(
                ModelBudgetPolicy.project_id == data["project_id"]
            )
        )
        assert existing is not None
        await db.delete(existing)
        await db.commit()

        body = ModelBudgetPolicyWrite(
            project_limit_usd=Decimal(5),
            task_limit_usd=Decimal(2),
            run_limit_usd=Decimal(1),
            probe_project_limit_usd=Decimal(5),
            probe_task_limit_usd=Decimal(2),
            probe_run_limit_usd=Decimal(1),
            project_token_limit=1000,
            task_token_limit=500,
            run_token_limit=250,
            probe_project_token_limit=1000,
            probe_task_token_limit=500,
            probe_run_token_limit=250,
            max_steps=12,
            max_wall_seconds=300,
            max_output_tokens=100,
            max_attempts=2,
        )
        created = await model_routes.set_project_model_budget(
            data["project_id"], body, user, db
        )
        assert created.project_limit_usd == Decimal(5)
        updated = await model_routes.set_project_model_budget(
            data["project_id"], body.model_copy(update={"max_steps": 16}), user, db
        )
        assert updated.max_steps == 16
        fetched = await model_routes.get_project_model_budget(
            data["project_id"], user, db
        )
        assert fetched.max_attempts == 2

        request_id = uuid4()
        db.add_all(
            [
                ModelRequestLedger(
                    request_id=request_id,
                    event_type="settled",
                    organization_id=data["organization_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    run_id=data["run_id"],
                    pool_name="primary",
                    model_alias="coding",
                    route_decision="primary",
                    usage_known=False,
                    billed_usd=Decimal("0.10"),
                    estimated_usd=Decimal("0.12"),
                    details={"unknown_usage": True},
                ),
                ModelRequestLedger(
                    request_id=request_id,
                    attempt=1,
                    event_type="attempt_succeeded",
                    organization_id=data["organization_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    run_id=data["run_id"],
                    pool_name="primary",
                    model_alias="coding",
                    route_decision="primary",
                    usage_known=True,
                    input_tokens=7,
                    output_tokens=4,
                    details={},
                ),
            ]
        )
        await db.commit()
        usage = await model_routes.get_project_model_usage(
            data["project_id"], user, db
        )
        events = await model_routes.list_project_model_requests(
            data["project_id"], limit=10, user=user, db=db
        )
        assert usage.billed_usd == Decimal("0.10")
        assert usage.estimated_usd == Decimal("0.12")
        assert (usage.input_tokens, usage.output_tokens) == (7, 4)
        assert usage.unknown_usage_requests == 1
        assert usage.pending_reservations == 0
        assert len(events) == 2

        with pytest.raises(HTTPException) as no_budget:
            await model_routes.get_project_model_budget(uuid4(), user, db)
        assert no_budget.value.status_code == 403

        with pytest.raises(HTTPException) as unscoped_request:
            await model_routes.model_request(
                data["project_id"],
                model_request(data).model_copy(update={"task_id": None}),
                "unscoped",
                user,
                db,
            )
        assert unscoped_request.value.status_code == 422

        stream_body = model_request(data, streaming=True)
        with pytest.raises(HTTPException) as wrong_endpoint:
            await model_routes.model_request(
                data["project_id"], stream_body, "wrong-route", user, db
            )
        assert wrong_endpoint.value.status_code == 400


@pytest.mark.anyio
async def test_model_routes_stub_router_and_live_check_fail_closed_without_spending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await gateway_fixture()
    user = await _admin(data)
    request_id = uuid4()
    call_parameters: list[dict[str, object]] = []

    async def complete(_db, **kwargs):
        call_parameters.append(kwargs)
        return {
            "request_id": request_id,
            "deployment": "fixture-deployment",
            "provider_account": "fixture-account",
            "actual_model": "provider/fixture",
            "usage_known": True,
            "billed_usd": Decimal("0.01"),
            "estimated_usd": Decimal("0.01"),
            "response": {"choices": []},
            "route_decisions": ["primary"],
            "model_alias": "coding",
        }

    async def stream(_db, **kwargs):
        call_parameters.append(kwargs)

        async def chunks():
            yield b"event: model.delta\\ndata: fixture\\n\\n"

        return request_id, chunks()

    monkeypatch.setattr(model_routes.model_router, "complete", complete)
    monkeypatch.setattr(model_routes.model_router, "stream", stream)
    async with SessionLocal() as db:
        body = model_request(data)
        result = await model_routes.model_request(
            data["project_id"], body, "fixture-complete", user, db
        )
        assert result["request_id"] == request_id
        assert call_parameters[-1]["idempotency_key"] == "fixture-complete"

        response = await model_routes.model_stream(
            data["project_id"],
            model_request(data, streaming=True),
            "fixture-stream",
            user,
            db,
        )
        assert response.media_type == "text/event-stream"
        assert response.headers["X-Model-Request-ID"] == str(request_id)
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks == [b"event: model.delta\\ndata: fixture\\n\\n"]

        with pytest.raises(HTTPException) as unauthorized_spend:
            await model_routes.live_route_check(
                data["project_id"],
                ModelLiveCheckRequest(alias="coding", authorize_paid_request=False),
                "no-spend",
                user,
                db,
            )
        assert unauthorized_spend.value.status_code == 400
        live = await model_routes.live_route_check(
            data["project_id"],
            ModelLiveCheckRequest(alias="coding", authorize_paid_request=True),
            "authorized-fixture-only",
            user,
            db,
        )
        assert live.outcome == "passed"
        assert live.request_id == request_id
        assert call_parameters[-1]["request"].estimated_input_tokens == 32

        async def unavailable(_db, **kwargs):
            raise ModelGatewayError("route_unavailable", 503, "fixture route unavailable")

        monkeypatch.setattr(model_routes.model_router, "complete", unavailable)
        with pytest.raises(HTTPException) as unavailable_route:
            await model_routes.model_request(
                data["project_id"], body, "fixture-error", user, db
            )
        assert unavailable_route.value.status_code == 503
        failed_live = await model_routes.live_route_check(
            data["project_id"],
            ModelLiveCheckRequest(alias="coding", authorize_paid_request=True),
            "authorized-fixture-failure",
            user,
            db,
        )
        assert failed_live.outcome == "failed"
        assert failed_live.error_code == "route_unavailable"


@pytest.mark.anyio
async def test_model_route_conflicts_authorization_and_doctor_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await gateway_fixture()
    user = await _admin(data)
    async with SessionLocal() as db:
        primary = await db.get(ProviderAccount, data["primary_account_id"])
        assert primary is not None
        foreign_org = Organization(name=f"Foreign {uuid4().hex[:8]}")
        db.add(foreign_org)
        await db.flush()
        foreign_account = ProviderAccount(
            organization_id=foreign_org.id,
            name="foreign-account",
            provider="openai",
            secret_ref="env:FOREIGN_KEY",
            enabled=True,
        )
        db.add(foreign_account)
        await db.commit()
        foreign_account_id = foreign_account.id

        denied_body = ModelDeploymentCreate(
            name="foreign-deployment",
            provider_account_id=foreign_account_id,
            actual_model="provider/foreign",
            proxy_model="gateway-foreign",
            capabilities=ModelDeploymentCapabilities(
                protocols=[ModelProtocol.CHAT_COMPLETIONS]
            ),
            context_window=1024,
            input_cost_per_million=Decimal(0),
            output_cost_per_million=Decimal(0),
            price_revision="foreign-price",
        )
        with pytest.raises(HTTPException) as wrong_org_deployment:
            await model_routes.create_deployment(denied_body, user, db)
        assert wrong_org_deployment.value.status_code == 404

        with pytest.raises(HTTPException) as missing_deployments:
            await model_routes.create_alias(
                ModelAliasCreate(name="foreign-alias", deployment_ids=[uuid4()]),
                user,
                db,
            )
        assert missing_deployments.value.status_code == 422

        pool = await db.get(model_routes.ModelPool, data["interactive_pool_id"])
        assert pool is not None
        with pytest.raises(HTTPException) as absent_pool:
            await model_routes.add_pool_route(
                uuid4(),
                ModelPoolRouteCreate(model_alias_id=uuid4(), priority=0),
                user,
                db,
            )
        assert absent_pool.value.status_code == 404
        with pytest.raises(HTTPException) as absent_alias:
            await model_routes.add_pool_route(
                pool.id,
                ModelPoolRouteCreate(model_alias_id=uuid4(), priority=0),
                user,
                db,
            )
        assert absent_alias.value.status_code == 404

        existing_alias = await db.scalar(
            select(model_routes.ModelAlias).where(
                model_routes.ModelAlias.name == "coding"
            )
        )
        assert existing_alias is not None
        with pytest.raises(HTTPException) as duplicate_pool_route:
            await model_routes.add_pool_route(
                pool.id,
                ModelPoolRouteCreate(model_alias_id=existing_alias.id, priority=0),
                user,
                db,
            )
        assert duplicate_pool_route.value.status_code == 409

        organization_membership = await db.scalar(
            select(Membership).where(Membership.user_id == user.id)
        )
        assert organization_membership is not None
        organization_membership.role = "operator"
        await db.commit()
        with pytest.raises(HTTPException) as org_admin_required:
            await model_routes.get_model_configuration(user, db)
        assert org_admin_required.value.status_code == 403
        organization_membership.role = "administrator"
        await db.commit()

        empty_org = Organization(name=f"Empty {uuid4().hex[:8]}")
        db.add(empty_org)
        await db.commit()
        monkeypatch.setattr(model_routes.settings, "model_gateway_url", "")
        empty_status = await model_routes._doctor(empty_org.id, db)
        assert empty_status.configured_accounts == 0
        assert empty_status.configured_deployments == 0
        assert empty_status.configured_aliases == 0
        assert len(empty_status.findings) == 4

        class ReadyResponse:
            is_success = True

        class ReadyClient:
            def __init__(self, *, timeout: float) -> None:
                assert timeout == 2.0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def get(self, url: str, *, headers: dict[str, str] | None = None):
                if url == "http://gateway.internal/health/readiness":
                    assert headers is None
                    return ReadyResponse()
                assert url == "http://gateway.internal/v1/models"
                assert headers == {"Authorization": "Bearer sk-fixture-virtual-key"}
                return ReadyResponse()

        monkeypatch.setattr(model_routes.settings, "model_gateway_url", "http://gateway.internal/")
        monkeypatch.setattr(model_routes.httpx, "AsyncClient", ReadyClient)
        monkeypatch.setattr(model_routes.settings, "model_gateway_api_key", None)
        missing_key = await model_routes._doctor(data["organization_id"], db)
        assert missing_key.status == "setup_required"
        assert any("MODEL_GATEWAY_API_KEY" in item for item in missing_key.findings)
        monkeypatch.setattr(
            model_routes.settings,
            "model_gateway_api_key",
            SecretStr("sk-fixture-virtual-key"),
        )
        ready = await model_routes._doctor(data["organization_id"], db)
        assert ready.status == "ready"
        assert ready.gateway_ready is True
        assert ready.findings == []

        class UnavailableClient(ReadyClient):
            async def get(self, _url: str, *, headers: dict[str, str] | None = None):
                raise model_routes.httpx.ConnectError("offline fixture")

        monkeypatch.setattr(model_routes.httpx, "AsyncClient", UnavailableClient)
        unavailable = await model_routes._doctor(data["organization_id"], db)
        assert unavailable.gateway_ready is False
        assert "authenticated model-list check" in unavailable.findings[0]

        reader = User(
            email=f"model-reader-{uuid4().hex[:8]}@example.test",
            hashed_password="not-used-for-direct-route-test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(reader)
        await db.flush()
        organization_membership = await db.scalar(
            select(Membership).where(Membership.user_id == user.id)
        )
        assert organization_membership is not None
        db.add(Membership(organization_id=data["organization_id"], user_id=reader.id, role="reviewer"))
        db.add(ProjectMembership(project_id=data["project_id"], user_id=reader.id, role="viewer"))
        await db.commit()
        with pytest.raises(HTTPException) as budget_forbidden:
            await model_routes.set_project_model_budget(
                data["project_id"],
                ModelBudgetPolicyWrite(
                    project_limit_usd=Decimal(1),
                    task_limit_usd=Decimal(1),
                    run_limit_usd=Decimal(1),
                    probe_project_limit_usd=Decimal(1),
                    probe_task_limit_usd=Decimal(1),
                    probe_run_limit_usd=Decimal(1),
                    project_token_limit=100,
                    task_token_limit=100,
                    run_token_limit=100,
                    probe_project_token_limit=100,
                    probe_task_token_limit=100,
                    probe_run_token_limit=100,
                    max_steps=1,
                    max_wall_seconds=60,
                    max_output_tokens=10,
                ),
                reader,
                db,
            )
        assert budget_forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as route_forbidden:
            await model_routes.model_request(
                data["project_id"], model_request(data), "reader-request", reader, db
            )
        assert route_forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as stream_forbidden:
            await model_routes.model_stream(
                data["project_id"],
                model_request(data, streaming=True),
                "reader-stream",
                reader,
                db,
            )
        assert stream_forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as live_forbidden:
            await model_routes.live_route_check(
                data["project_id"],
                ModelLiveCheckRequest(alias="coding", authorize_paid_request=True),
                "reader-live-check",
                reader,
                db,
            )
        assert live_forbidden.value.status_code == 403
