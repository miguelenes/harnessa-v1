"""Administrative route configuration and project-scoped model budget APIs."""

from decimal import Decimal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    ModelAliasCreate,
    ModelAliasRead,
    ModelBudgetPolicyRead,
    ModelBudgetPolicyWrite,
    ModelDeploymentCreate,
    ModelDeploymentRead,
    ModelDoctorRead,
    ModelGatewayConfiguration,
    ModelInvocationRead,
    ModelLiveCheckRead,
    ModelLiveCheckRequest,
    ModelMessage,
    ModelPoolKind,
    ModelPoolRead,
    ModelPoolRouteCreate,
    ModelProtocol,
    ModelProviderAccountCreate,
    ModelProviderAccountRead,
    ModelRequestLedgerRead,
    ModelRouteRequest,
    ModelUsageRead,
    RouteRequirements,
)
from harnessa.db.model_gateway import (
    AliasDeployment,
    ModelAlias,
    ModelBudgetPolicy,
    ModelDeployment,
    ModelPool,
    ModelRequestLedger,
    ModelReservation,
    PoolAlias,
    ProviderAccount,
)
from harnessa.db.models import Membership, Project, User
from harnessa.db.session import get_db
from harnessa.services.authorization import require_membership
from harnessa.services.model_gateway import LiteLLMModelRouter, ModelGatewayError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["model gateway"])
model_router = LiteLLMModelRouter()


async def _organization_admin(db: AsyncSession, user: User) -> UUID:
    membership = await require_membership(db, user)
    if not isinstance(membership, Membership) or membership.role != "administrator":
        raise HTTPException(403, "Organization administrator role required")
    return membership.organization_id


@router.get("/ops/models/config", response_model=ModelGatewayConfiguration)
async def get_model_configuration(
    user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ModelGatewayConfiguration:
    organization_id = await _organization_admin(db, user)
    pools = (
        await db.execute(
            select(ModelPool)
            .where(ModelPool.organization_id == organization_id)
            .order_by(ModelPool.kind)
        )
    ).scalars().all()
    accounts = (
        await db.execute(
            select(ProviderAccount)
            .where(ProviderAccount.organization_id == organization_id)
            .order_by(ProviderAccount.name)
        )
    ).scalars().all()
    deployments = (
        await db.execute(
            select(ModelDeployment)
            .where(ModelDeployment.organization_id == organization_id)
            .order_by(ModelDeployment.name)
        )
    ).scalars().all()
    aliases = (
        await db.execute(
            select(ModelAlias)
            .where(ModelAlias.organization_id == organization_id)
            .order_by(ModelAlias.name)
        )
    ).scalars().all()
    alias_reads: list[ModelAliasRead] = []
    for alias in aliases:
        deployment_ids = (
            await db.execute(
                select(AliasDeployment.deployment_id)
                .where(AliasDeployment.alias_id == alias.id)
                .order_by(AliasDeployment.priority)
            )
        ).scalars().all()
        alias_reads.append(
            ModelAliasRead(
                id=alias.id,
                name=alias.name,
                description=alias.description,
                enabled=alias.enabled,
                deployment_ids=list(deployment_ids),
            )
        )
    return ModelGatewayConfiguration(
        pools=[ModelPoolRead.model_validate(item) for item in pools],
        accounts=[ModelProviderAccountRead.model_validate(item) for item in accounts],
        deployments=[ModelDeploymentRead.model_validate(item) for item in deployments],
        aliases=alias_reads,
    )


@router.post("/ops/models/accounts", response_model=ModelProviderAccountRead, status_code=201)
async def create_provider_account(
    body: ModelProviderAccountCreate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderAccount:
    organization_id = await _organization_admin(db, user)
    account = ProviderAccount(
        organization_id=organization_id,
        name=body.name,
        provider=body.provider,
        secret_ref=body.secret_ref,
        enabled=True,
    )
    db.add(account)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Provider account name already exists") from exc
    await db.refresh(account)
    return account


@router.post("/ops/models/deployments", response_model=ModelDeploymentRead, status_code=201)
async def create_deployment(
    body: ModelDeploymentCreate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelDeployment:
    organization_id = await _organization_admin(db, user)
    account = await db.get(ProviderAccount, body.provider_account_id)
    if account is None or account.organization_id != organization_id:
        raise HTTPException(404, "Provider account not found")
    deployment = ModelDeployment(
        organization_id=organization_id,
        provider_account_id=account.id,
        name=body.name,
        actual_model=body.actual_model,
        proxy_model=body.proxy_model,
        capabilities=body.capabilities.model_dump(mode="json"),
        context_window=body.context_window,
        input_cost_per_million=body.input_cost_per_million,
        output_cost_per_million=body.output_cost_per_million,
        price_revision=body.price_revision,
        max_concurrency=body.max_concurrency,
        enabled=True,
    )
    db.add(deployment)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Deployment name or LiteLLM model is already registered") from exc
    await db.refresh(deployment)
    return deployment


@router.post("/ops/models/aliases", response_model=ModelAliasRead, status_code=201)
async def create_alias(
    body: ModelAliasCreate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelAliasRead:
    organization_id = await _organization_admin(db, user)
    deployments = (
        await db.execute(
            select(ModelDeployment).where(
                ModelDeployment.organization_id == organization_id,
                ModelDeployment.id.in_(body.deployment_ids),
            )
        )
    ).scalars().all()
    if len(deployments) != len(set(body.deployment_ids)):
        raise HTTPException(422, "Every route deployment must belong to this organization")
    alias = ModelAlias(
        organization_id=organization_id,
        name=body.name,
        description=body.description,
        enabled=True,
    )
    db.add(alias)
    try:
        await db.flush()
        db.add_all(
            [
                AliasDeployment(alias_id=alias.id, deployment_id=deployment_id, priority=priority)
                for priority, deployment_id in enumerate(body.deployment_ids)
            ]
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Model alias already exists or has invalid deployment order") from exc
    return ModelAliasRead(
        id=alias.id,
        name=alias.name,
        description=alias.description,
        enabled=alias.enabled,
        deployment_ids=body.deployment_ids,
    )


@router.post("/ops/models/pools/{pool_id}/routes", status_code=201)
async def add_pool_route(
    pool_id: UUID,
    body: ModelPoolRouteCreate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | int]:
    organization_id = await _organization_admin(db, user)
    pool = await db.get(ModelPool, pool_id)
    alias = await db.get(ModelAlias, body.model_alias_id)
    if pool is None or pool.organization_id != organization_id:
        raise HTTPException(404, "Model pool not found")
    if alias is None or alias.organization_id != organization_id:
        raise HTTPException(404, "Model alias not found")
    route = PoolAlias(pool_id=pool.id, alias_id=alias.id, priority=body.priority)
    db.add(route)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Pool route or priority already exists") from exc
    return {"pool": pool.name, "alias": alias.name, "priority": body.priority}


@router.get("/ops/models/status", response_model=ModelDoctorRead)
async def model_status(
    user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ModelDoctorRead:
    return await _doctor(await _organization_admin(db, user), db)


@router.get("/ops/models/doctor", response_model=ModelDoctorRead)
async def model_doctor(
    user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ModelDoctorRead:
    return await _doctor(await _organization_admin(db, user), db)


async def _doctor(organization_id: UUID, db: AsyncSession) -> ModelDoctorRead:
    pools = (
        await db.execute(
            select(ModelPool).where(ModelPool.organization_id == organization_id).order_by(ModelPool.kind)
        )
    ).scalars().all()
    account_count = await db.scalar(
        select(func.count()).select_from(ProviderAccount).where(
            ProviderAccount.organization_id == organization_id
        )
    )
    deployment_count = await db.scalar(
        select(func.count()).select_from(ModelDeployment).where(
            ModelDeployment.organization_id == organization_id
        )
    )
    alias_count = await db.scalar(
        select(func.count()).select_from(ModelAlias).where(ModelAlias.organization_id == organization_id)
    )
    gateway_ready = False
    if settings.model_gateway_url:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(
                    f"{settings.model_gateway_url.rstrip('/')}/health/readiness"
                )
                gateway_ready = response.is_success
        except httpx.HTTPError:
            gateway_ready = False
    findings: list[str] = []
    if not settings.model_gateway_url:
        findings.append("Set MODEL_GATEWAY_URL to the internal LiteLLM Proxy URL.")
    elif not gateway_ready:
        findings.append("LiteLLM Proxy readiness endpoint did not return success.")
    if not deployment_count:
        findings.append("Register at least one LiteLLM deployment with an explicit secret reference and price revision.")
    if not alias_count:
        findings.append("Create a capability-checked model alias and add it to a workload pool.")
    if not pools:
        findings.append("Create the default interactive/primary and probe/batch pools by bootstrapping the administrator.")
    return ModelDoctorRead(
        status="ready" if gateway_ready and deployment_count and alias_count and pools else "setup_required",
        gateway_ready=gateway_ready,
        configured_accounts=int(account_count or 0),
        configured_deployments=int(deployment_count or 0),
        configured_aliases=int(alias_count or 0),
        pools=[pool.name for pool in pools],
        provider_credentials_checked=False,
        live_request_sent=False,
        findings=findings,
    )


@router.put("/projects/{project_id}/models/budget", response_model=ModelBudgetPolicyRead)
async def set_project_model_budget(
    project_id: UUID,
    body: ModelBudgetPolicyWrite,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelBudgetPolicy:
    membership = await require_membership(db, user, project_id)
    if membership.role not in ("administrator", "operator"):
        raise HTTPException(403, "Project operator role required")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    policy = (
        await db.execute(
            select(ModelBudgetPolicy)
            .where(ModelBudgetPolicy.project_id == project_id)
            .with_for_update()
        )
    ).scalars().first()
    values = body.model_dump()
    if policy is None:
        policy = ModelBudgetPolicy(organization_id=project.organization_id, project_id=project_id, **values)
        db.add(policy)
    else:
        for key, value in values.items():
            setattr(policy, key, value)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.get("/projects/{project_id}/models/budget", response_model=ModelBudgetPolicyRead)
async def get_project_model_budget(
    project_id: UUID,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelBudgetPolicy:
    await require_membership(db, user, project_id)
    policy = (
        await db.execute(select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id))
    ).scalars().first()
    if policy is None:
        raise HTTPException(404, "Model budget has not been configured")
    return policy


@router.get("/projects/{project_id}/models/usage", response_model=ModelUsageRead)
async def get_project_model_usage(
    project_id: UUID,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelUsageRead:
    await require_membership(db, user, project_id)
    events = (
        await db.execute(
            select(ModelRequestLedger).where(
                ModelRequestLedger.project_id == project_id,
                ModelRequestLedger.event_type.in_(("settled", "stale_reconciled", "attempt_succeeded")),
            )
        )
    ).scalars().all()
    request_totals = [
        item for item in events if item.event_type in ("settled", "stale_reconciled")
    ]
    successful_attempts = [item for item in events if item.event_type == "attempt_succeeded"]
    unknown = sum(
        1
        for item in request_totals
        if item.details.get("unknown_usage") is True or item.event_type == "stale_reconciled"
    )
    pending = await db.scalar(
        select(func.count()).select_from(ModelReservation).where(
            ModelReservation.project_id == project_id,
            ModelReservation.status == "pending",
        )
    )
    return ModelUsageRead(
        project_id=project_id,
        billed_usd=sum((item.billed_usd or Decimal(0) for item in request_totals), Decimal(0)),
        estimated_usd=sum((item.estimated_usd or Decimal(0) for item in request_totals), Decimal(0)),
        input_tokens=sum((item.input_tokens or 0 for item in successful_attempts), 0),
        output_tokens=sum((item.output_tokens or 0 for item in successful_attempts), 0),
        unknown_usage_requests=unknown,
        pending_reservations=int(pending or 0),
    )


@router.get("/projects/{project_id}/models/requests", response_model=list[ModelRequestLedgerRead])
async def list_project_model_requests(
    project_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModelRequestLedger]:
    await require_membership(db, user, project_id)
    return list(
        (
            await db.execute(
                select(ModelRequestLedger)
                .where(ModelRequestLedger.project_id == project_id)
                .order_by(ModelRequestLedger.created_at.desc(), ModelRequestLedger.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


@router.post("/projects/{project_id}/models/requests", response_model=ModelInvocationRead)
async def model_request(
    project_id: UUID,
    body: ModelRouteRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    membership = await require_membership(db, user, project_id)
    if membership.role not in ("administrator", "operator"):
        raise HTTPException(403, "Project operator role required")
    if body.stream:
        raise HTTPException(400, "Send stream=true requests to /models/requests/stream")
    if body.task_id is None or body.run_id is None:
        raise HTTPException(422, "Normal model requests must be scoped to both a task and active run")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    try:
        return await model_router.complete(
            db,
            organization_id=project.organization_id,
            project_id=project_id,
            request=body,
            idempotency_key=idempotency_key,
        )
    except ModelGatewayError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/projects/{project_id}/models/requests/stream")
async def model_stream(
    project_id: UUID,
    body: ModelRouteRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    membership = await require_membership(db, user, project_id)
    if membership.role not in ("administrator", "operator"):
        raise HTTPException(403, "Project operator role required")
    if body.task_id is None or body.run_id is None:
        raise HTTPException(422, "Normal model requests must be scoped to both a task and active run")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    try:
        request_id, stream = await model_router.stream(
            db,
            organization_id=project.organization_id,
            project_id=project_id,
            request=body,
            idempotency_key=idempotency_key,
        )
    except ModelGatewayError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"X-Model-Request-ID": str(request_id), "Cache-Control": "no-cache"},
    )


@router.post("/projects/{project_id}/models/live-check", response_model=ModelLiveCheckRead)
async def live_route_check(
    project_id: UUID,
    body: ModelLiveCheckRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ModelLiveCheckRead:
    membership = await require_membership(db, user, project_id)
    if not isinstance(membership, Membership) or membership.role != "administrator":
        raise HTTPException(403, "Organization administrator role required for a paid route check")
    if not body.authorize_paid_request:
        raise HTTPException(400, "Explicit spending authorization is required")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    request = ModelRouteRequest(
        pool=ModelPoolKind.INTERACTIVE,
        model_alias=body.alias,
        requirements=RouteRequirements(protocol=ModelProtocol.CHAT_COMPLETIONS, minimum_context=64),
        max_output_tokens=1,
        estimated_input_tokens=32,
        messages=[ModelMessage(role="user", content="Reply with OK.")],
    )
    try:
        result = await model_router.complete(
            db,
            organization_id=project.organization_id,
            project_id=project_id,
            request=request,
            idempotency_key=idempotency_key,
        )
        return ModelLiveCheckRead(
            outcome="passed",
            request_id=result["request_id"],
            deployment=result["deployment"],
            provider_account=result["provider_account"],
            actual_model=result["actual_model"],
            usage_known=result["usage_known"],
            billed_usd=result["billed_usd"],
            estimated_usd=result["estimated_usd"],
        )
    except ModelGatewayError as exc:
        return ModelLiveCheckRead(
            outcome="failed",
            request_id=UUID(int=0),
            error_code=exc.code,
        )
