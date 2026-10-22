"""Capability-aware LiteLLM routing with atomic budgets and a redacted audit ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from harnessa.config import settings
from harnessa.contracts import ModelProtocol, ModelRouteRequest, RouteRequirements
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
from harnessa.db.models import Project, Task
from harnessa.db.orchestration import Run
from harnessa.db.session import SessionLocal
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("harnessa.model_gateway")
_ZERO = Decimal(0)
_MILLION = Decimal(1000000)
_MIN_RESERVATION = Decimal("0.00000001")


def _reservation_lease_seconds() -> int:
    return max(
        settings.model_reservation_lease_seconds,
        int(settings.model_http_timeout_seconds) + 30,
    )


class ModelGatewayError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class RouteCandidate:
    alias: ModelAlias
    deployment: ModelDeployment
    account: ProviderAccount
    pool_priority: int
    deployment_priority: int


@dataclass(frozen=True)
class ReservationContext:
    reservation: ModelReservation
    pool: ModelPool
    policy: ModelBudgetPolicy
    candidates: list[RouteCandidate]
    request_id: UUID
    per_attempt_usd: Decimal
    per_attempt_tokens: int
    max_attempts: int


@dataclass(frozen=True)
class AttemptOutcome:
    response: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    usage_known: bool
    billed_usd: Decimal | None
    estimated_usd: Decimal | None
    request_identifier: str | None
    actual_model: str
    latency_ms: int
    error_code: str | None = None


def _protocol_path(protocol: ModelProtocol) -> str:
    return {
        ModelProtocol.CHAT_COMPLETIONS: "/v1/chat/completions",
        ModelProtocol.RESPONSES: "/v1/responses",
        ModelProtocol.ANTHROPIC_MESSAGES: "/v1/messages",
    }[protocol]


def _supports(capabilities: dict[str, Any], requirements: RouteRequirements) -> bool:
    protocols = capabilities.get("protocols", [])
    protocol_names = [p.value if isinstance(p, ModelProtocol) else str(p) for p in protocols]
    return (
        requirements.protocol.value in protocol_names
        and (not requirements.streaming or capabilities.get("streaming") is True)
        and (not requirements.tools or capabilities.get("tools") is True)
        and (not requirements.usage_reporting or capabilities.get("usage_reporting") is True)
        and (not requirements.cancellation or capabilities.get("cancellation") is True)
    )


async def _configured_route_candidates(
    db: AsyncSession,
    *,
    organization_id: UUID,
    pool_kind: str,
    alias_name: str | None,
    requirements: RouteRequirements,
) -> tuple[ModelPool, list[RouteCandidate]]:
    pool = (
        await db.execute(
            select(ModelPool).where(
                ModelPool.organization_id == organization_id,
                ModelPool.kind == pool_kind,
            )
        )
    ).scalars().first()
    if pool is None:
        raise ModelGatewayError("model_pool_not_configured", 503, "The model workload pool is not configured")

    stmt = (
        select(ModelAlias, ModelDeployment, ProviderAccount, PoolAlias.priority, AliasDeployment.priority)
        .join(PoolAlias, PoolAlias.alias_id == ModelAlias.id)
        .join(AliasDeployment, AliasDeployment.alias_id == ModelAlias.id)
        .join(ModelDeployment, ModelDeployment.id == AliasDeployment.deployment_id)
        .join(ProviderAccount, ProviderAccount.id == ModelDeployment.provider_account_id)
        .where(PoolAlias.pool_id == pool.id, ModelAlias.enabled.is_(True))
        .order_by(PoolAlias.priority, AliasDeployment.priority)
    )
    if alias_name:
        stmt = stmt.where(ModelAlias.name == alias_name)
    rows = (await db.execute(stmt)).all()
    matching = [
        RouteCandidate(alias, deployment, account, pool_priority, deployment_priority)
        for alias, deployment, account, pool_priority, deployment_priority in rows
        if _supports(deployment.capabilities, requirements)
        and deployment.context_window >= requirements.minimum_context
    ]
    if not matching:
        if alias_name and not rows:
            detail = f"Alias '{alias_name}' is not an authorized route in this workload pool"
        else:
            detail = "No configured route satisfies every requested protocol and capability"
        raise ModelGatewayError("no_authorized_compatible_route", 422, detail)

    enabled = [
        item
        for item in matching
        if item.account.enabled
        and item.deployment.enabled
        and item.account.secret_ref.startswith("env:")
    ]
    if not enabled:
        raise ModelGatewayError(
            "no_authorized_compatible_route", 503, "All compatible provider accounts or deployments are disabled"
        )
    return pool, enabled


async def has_compatible_configured_route(
    db: AsyncSession,
    *,
    organization_id: UUID,
    pool_kind: str,
    alias_name: str | None,
    requirements: RouteRequirements,
) -> bool:
    """Check configured route metadata without reserving capacity or contacting a provider."""
    try:
        await _configured_route_candidates(
            db,
            organization_id=organization_id,
            pool_kind=pool_kind,
            alias_name=alias_name,
            requirements=requirements,
        )
    except ModelGatewayError:
        return False
    return True


async def route_candidates(
    db: AsyncSession,
    *,
    organization_id: UUID,
    pool_kind: str,
    alias_name: str | None,
    requirements: RouteRequirements,
) -> tuple[ModelPool, list[RouteCandidate]]:
    pool, enabled = await _configured_route_candidates(
        db,
        organization_id=organization_id,
        pool_kind=pool_kind,
        alias_name=alias_name,
        requirements=requirements,
    )
    now = datetime.now(UTC)
    available = [
        item
        for item in enabled
        if item.deployment.cooldown_until is None or item.deployment.cooldown_until <= now
    ]
    if not available:
        raise ModelGatewayError(
            "no_available_compatible_route", 503, "All authorized compatible routes are cooling down"
        )
    return pool, available


def estimate_cost(
    candidates: list[RouteCandidate], input_tokens: int, output_tokens: int
) -> Decimal:
    estimates = [
        (
            Decimal(input_tokens) * item.deployment.input_cost_per_million
            + Decimal(output_tokens) * item.deployment.output_cost_per_million
        )
        / _MILLION
        for item in candidates
    ]
    return max([_MIN_RESERVATION, *estimates])


def _scope_values(
    project_id: UUID, task_id: UUID | None, run_id: UUID | None
) -> list[tuple[str, UUID, UUID | None, UUID | None]]:
    result: list[tuple[str, UUID, UUID | None, UUID | None]] = [
        ("project", project_id, None, None)
    ]
    if task_id is not None:
        result.append(("task", task_id, task_id, None))
    if run_id is not None:
        result.append(("run", run_id, task_id, run_id))
    return result


async def _extend_reservation_lease(db: AsyncSession, reservation_id: UUID) -> None:
    """Renew a pending reservation immediately before a bounded provider attempt."""
    now = datetime.now(UTC)
    result = await db.execute(
        update(ModelReservation)
        .where(
            ModelReservation.id == reservation_id,
            ModelReservation.status == "pending",
            ModelReservation.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=_reservation_lease_seconds())
        )
        .returning(ModelReservation.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.scalar_one_or_none() is None:
        raise ModelGatewayError(
            "model_reservation_expired", 409, "The model request reservation expired before dispatch"
        )


async def _reservation_heartbeat(request_id: UUID, max_wall_seconds: int) -> None:
    """Keep a streaming reservation alive while its response body is consumed."""
    lease_seconds = _reservation_lease_seconds()
    interval = max(1.0, lease_seconds / 3)
    deadline = asyncio.get_running_loop().time() + max_wall_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(interval)
        now = datetime.now(UTC)
        async with SessionLocal() as db:
            result = await db.execute(
                update(ModelReservation)
                .where(
                    ModelReservation.request_id == request_id,
                    ModelReservation.status == "pending",
                    ModelReservation.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds)
                )
                .returning(ModelReservation.id)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if result.scalar_one_or_none() is None:
                return


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _reserve(
    db: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID,
    task_id: UUID | None,
    run_id: UUID | None,
    pool: ModelPool,
    candidates: list[RouteCandidate],
    request: ModelRouteRequest,
    idempotency_key: str,
) -> ReservationContext:
    if not idempotency_key or len(idempotency_key) > 200:
        raise ModelGatewayError("idempotency_key_required", 400, "A valid Idempotency-Key is required")
    if request.max_output_tokens > 1_000_000:
        raise ModelGatewayError("invalid_token_limit", 422, "Output token limit exceeds the supported maximum")

    policy = (
        await db.execute(
            select(ModelBudgetPolicy)
            .where(ModelBudgetPolicy.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if policy is None:
        raise ModelGatewayError(
            "model_budget_not_configured", 409, "Configure project, task, and run budgets before dispatch"
        )
    candidates = [
        item
        for item in candidates
        if request.estimated_input_tokens + request.max_output_tokens
        <= item.deployment.context_window
    ]
    if not candidates:
        raise ModelGatewayError(
            "no_authorized_compatible_route", 422, "No route can hold the estimated input and output token limits"
        )
    if request.max_output_tokens > policy.max_output_tokens:
        raise ModelGatewayError(
            "output_token_limit_exceeded", 409, "Requested output exceeds the configured budget policy"
        )
    max_attempts = min(policy.max_attempts, settings.model_max_attempts, 5)
    input_and_output_tokens = request.estimated_input_tokens + request.max_output_tokens
    per_attempt_usd = estimate_cost(candidates, request.estimated_input_tokens, request.max_output_tokens)
    per_attempt_tokens = input_and_output_tokens
    reserved_usd = per_attempt_usd * max_attempts
    reserved_tokens = per_attempt_tokens * max_attempts

    project = await db.get(Project, project_id)
    if project is None or project.organization_id != organization_id:
        raise ModelGatewayError("project_not_found", 404, "Project not found")
    if task_id is not None:
        task = await db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise ModelGatewayError("task_not_found", 404, "Task is not in this project")
        if (datetime.now(UTC) - task.created_at).total_seconds() > policy.max_wall_seconds:
            raise ModelGatewayError("task_time_limit_exceeded", 409, "Task has reached its wall-time limit")
    if run_id is not None:
        run = await db.get(Run, run_id)
        if run is None or run.project_id != project_id or run.task_id != task_id:
            raise ModelGatewayError("run_not_found", 404, "Run is not in this task and project")
        if run.status != "RUNNING":
            raise ModelGatewayError("run_not_active", 409, "Model calls require an active run")
        run_started = run.started_at or run.created_at
        if (datetime.now(UTC) - run_started).total_seconds() > policy.max_wall_seconds:
            raise ModelGatewayError("run_time_limit_exceeded", 409, "Run has reached its wall-time limit")

    idempotency_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    previous = (
        await db.execute(
            select(ModelReservation).where(
                ModelReservation.project_id == project_id,
                ModelReservation.idempotency_hash == idempotency_hash,
            )
        )
    ).scalars().first()
    if previous is not None:
        status = "in progress" if previous.status == "pending" else "already used"
        raise ModelGatewayError("model_request_replayed", 409, f"Idempotency-Key was {status}; request was not repeated")

    # The project policy lock serializes all scopes for this project, including parallel probes.
    limits = {
        "project": (Decimal(policy.project_limit_usd), policy.project_token_limit),
        "task": (Decimal(policy.task_limit_usd), policy.task_token_limit),
        "run": (Decimal(policy.run_limit_usd), policy.run_token_limit),
    }
    counters: list[ModelBudgetCounter] = []
    for scope, scope_id, counter_task_id, counter_run_id in _scope_values(project_id, task_id, run_id):
        counter = (
            await db.execute(
                select(ModelBudgetCounter)
                .where(
                    ModelBudgetCounter.project_id == project_id,
                    ModelBudgetCounter.scope == scope,
                    ModelBudgetCounter.scope_id == scope_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if counter is None:
            counter = ModelBudgetCounter(
                organization_id=organization_id,
                project_id=project_id,
                task_id=counter_task_id,
                run_id=counter_run_id,
                scope=scope,
                scope_id=scope_id,
                reserved_usd=_ZERO,
                billed_usd=_ZERO,
                estimated_usd=_ZERO,
                reserved_tokens=0,
                used_tokens=0,
                used_steps=0,
            )
            db.add(counter)
            await db.flush()
        counters.append(counter)

    for counter in counters:
        amount_limit, token_limit = limits[counter.scope]
        if counter.billed_usd + counter.estimated_usd + counter.reserved_usd + reserved_usd > amount_limit:
            raise ModelGatewayError("model_budget_exceeded", 409, f"{counter.scope.title()} spend ceiling would be exceeded")
        if counter.used_tokens + counter.reserved_tokens + reserved_tokens > token_limit:
            raise ModelGatewayError("model_token_limit_exceeded", 409, f"{counter.scope.title()} token ceiling would be exceeded")
        if counter.used_steps >= policy.max_steps:
            raise ModelGatewayError("model_step_limit_exceeded", 409, f"{counter.scope.title()} step limit is exhausted")

    capacity = (
        await db.execute(
            select(ModelPool)
            .where(ModelPool.organization_id == organization_id, ModelPool.kind == "interactive")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if capacity is None:
        raise ModelGatewayError("model_capacity_not_configured", 503, "Interactive model capacity is not configured")
    routable: list[RouteCandidate] = []
    for candidate in candidates:
        deployment = (
            await db.execute(
                select(ModelDeployment)
                .where(ModelDeployment.id == candidate.deployment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().one()
        if deployment.active_interactive + deployment.active_probe < deployment.max_concurrency:
            routable.append(candidate)
    candidates = routable
    if not candidates:
        raise ModelGatewayError("deployment_capacity_exhausted", 429, "All compatible deployments are currently at capacity")
    if request.estimated_input_tokens + request.max_output_tokens > candidates[0].deployment.context_window:
        raise ModelGatewayError("context_limit_exceeded", 422, "Estimated input and output exceed route context")
    normal_probe_capacity = max(0, capacity.max_concurrency - capacity.interactive_reserve)
    if pool.kind == "interactive":
        allowed_overflow = max(0, capacity.active_probe - normal_probe_capacity)
        if capacity.active_interactive + capacity.active_probe >= capacity.max_concurrency + allowed_overflow:
            raise ModelGatewayError("model_capacity_exhausted", 429, "Interactive model capacity is currently full")
        capacity.active_interactive += 1
        workload = "interactive"
    else:
        probe_limit = normal_probe_capacity + pool.probe_overflow_limit
        next_probe_count = capacity.active_probe + 1
        allowed_overflow = max(0, next_probe_count - normal_probe_capacity)
        if (
            capacity.active_probe >= probe_limit
            or capacity.active_interactive + next_probe_count > capacity.max_concurrency + allowed_overflow
        ):
            raise ModelGatewayError("probe_capacity_exhausted", 429, "Probe capacity is full; interactive capacity is reserved")
        capacity.active_probe += 1
        workload = "probe"

    deployment = candidates[0].deployment
    if workload == "interactive":
        deployment.active_interactive += 1
    else:
        deployment.active_probe += 1

    for counter in counters:
        counter.reserved_usd += reserved_usd
        counter.reserved_tokens += reserved_tokens
        counter.used_steps += 1

    request_id = uuid4()
    reservation = ModelReservation(
        organization_id=organization_id,
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        pool_id=pool.id,
        deployment_id=deployment.id,
        request_id=request_id,
        idempotency_hash=idempotency_hash,
        status="pending",
        reserved_usd=reserved_usd,
        per_attempt_usd=per_attempt_usd,
        reserved_tokens=reserved_tokens,
        per_attempt_tokens=per_attempt_tokens,
        max_attempts=max_attempts,
        workload=workload,
        lease_expires_at=datetime.now(UTC)
        + timedelta(seconds=_reservation_lease_seconds()),
    )
    db.add(reservation)
    db.add(
        ModelRequestLedger(
            request_id=request_id,
            attempt=0,
            event_type="reserved",
            organization_id=organization_id,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            pool_name=pool.name,
            model_alias=request.model_alias or candidates[0].alias.name,
            route_decision=f"reserved {max_attempts} bounded attempt(s) across {len(candidates)} compatible route(s)",
            usage_known=False,
            estimated_usd=reserved_usd,
            details={"reservation_status": "pending", "reserved_tokens": reserved_tokens},
        )
    )
    await db.commit()
    return ReservationContext(
        reservation,
        pool,
        policy,
        candidates,
        request_id,
        per_attempt_usd,
        per_attempt_tokens,
        max_attempts,
    )


async def _append_ledger(
    db: AsyncSession,
    context: ReservationContext,
    candidate: RouteCandidate | None,
    *,
    event_type: str,
    attempt: int,
    route_decision: str,
    request_identifier: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    usage_known: bool = False,
    latency_ms: int | None = None,
    billed_usd: Decimal | None = None,
    estimated_usd: Decimal | None = None,
    http_status: int | None = None,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    reservation = context.reservation
    db.add(
        ModelRequestLedger(
            request_id=context.request_id,
            attempt=attempt,
            event_type=event_type,
            organization_id=reservation.organization_id,
            project_id=reservation.project_id,
            task_id=reservation.task_id,
            run_id=reservation.run_id,
            account_id=candidate.account.id if candidate else None,
            deployment_id=candidate.deployment.id if candidate else None,
            pool_name=context.pool.name,
            model_alias=candidate.alias.name if candidate else "unknown",
            actual_model=candidate.deployment.actual_model if candidate else None,
            request_identifier=request_identifier,
            route_decision=route_decision[:200],
            price_revision=candidate.deployment.price_revision if candidate else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_known=usage_known,
            latency_ms=latency_ms,
            billed_usd=billed_usd,
            estimated_usd=estimated_usd,
            http_status=http_status,
            error_code=error_code,
            details=details or {},
        )
    )
    await db.commit()


async def _record_failure(
    db: AsyncSession,
    candidate: RouteCandidate,
    *,
    error_code: str,
    status_code: int | None,
    retry_after: float | None,
) -> datetime:
    deployment = (
        await db.execute(
            select(ModelDeployment)
            .where(ModelDeployment.id == candidate.deployment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    deployment.failure_count += 1
    deployment.last_failure_code = error_code
    now = datetime.now(UTC)
    cooldown = retry_after or (
        settings.model_cooldown_seconds
        if deployment.failure_count >= settings.model_circuit_breaker_failures or status_code == 429
        else 0
    )
    if cooldown > 0:
        proposed = now + timedelta(seconds=min(cooldown, 3600))
        if deployment.cooldown_until is None or proposed > deployment.cooldown_until:
            deployment.cooldown_until = proposed
    await db.commit()
    return deployment.cooldown_until or now


async def _record_success(db: AsyncSession, deployment_id: UUID) -> None:
    deployment = (
        await db.execute(
            select(ModelDeployment)
            .where(ModelDeployment.id == deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    deployment.failure_count = 0
    deployment.last_failure_code = None
    deployment.cooldown_until = None
    await db.commit()


async def _switch_deployment(
    db: AsyncSession, context: ReservationContext, candidate: RouteCandidate
) -> bool:
    """Move this request's deployment slot without changing its parent capacity slot."""
    reservation = (
        await db.execute(
            select(ModelReservation)
            .where(ModelReservation.id == context.reservation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    await db.execute(
        select(ModelPool)
        .where(
            ModelPool.organization_id == context.reservation.organization_id,
            ModelPool.kind == "interactive",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    deployments = (
        await db.execute(
            select(ModelDeployment)
            .where(ModelDeployment.id.in_([reservation.deployment_id, candidate.deployment.id]))
            .order_by(ModelDeployment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    by_id = {item.id: item for item in deployments}
    current = by_id[reservation.deployment_id]
    target = by_id[candidate.deployment.id]
    if target.id != current.id:
        if target.active_interactive + target.active_probe >= target.max_concurrency:
            await db.rollback()
            return False
        if target.cooldown_until is not None and target.cooldown_until > datetime.now(UTC):
            await db.rollback()
            return False
        if reservation.workload == "interactive":
            current.active_interactive = max(0, current.active_interactive - 1)
            target.active_interactive += 1
        else:
            current.active_probe = max(0, current.active_probe - 1)
            target.active_probe += 1
        reservation.deployment_id = target.id
    await db.commit()
    return True


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _error_code(status: int | None, exc: BaseException | None = None) -> str:
    if status == 429:
        return "provider_rate_limited"
    if status == 503:
        return "provider_unavailable"
    if status in {408, 504}:
        return "provider_timeout"
    if status is not None and status >= 500:
        return "provider_server_error"
    if status in {401, 403}:
        return "provider_authentication_failed"
    if status is not None:
        return "provider_request_rejected"
    if isinstance(exc, httpx.TimeoutException):
        return "provider_timeout"
    return "provider_transport_error"


def _cost_from_usage(
    candidate: RouteCandidate, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    if input_tokens is None or output_tokens is None:
        return None
    amount = (
        Decimal(input_tokens) * candidate.deployment.input_cost_per_million
        + Decimal(output_tokens) * candidate.deployment.output_cost_per_million
    ) / _MILLION
    return amount.quantize(Decimal("0.00000001"))


def _extract_usage(response: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None, None, False
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None, None, False
    try:
        input_count = int(input_tokens)
        output_count = int(output_tokens)
    except (TypeError, ValueError):
        return None, None, False
    if input_count < 0 or output_count < 0:
        return None, None, False
    return input_count, output_count, True


def _stream_usage(
    payload: str, protocol: ModelProtocol
) -> tuple[int | None, int | None] | None:
    if payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if protocol == ModelProtocol.RESPONSES:
        usage = (data.get("response") or {}).get("usage") or usage
    if not isinstance(usage, dict):
        return None
    input_value = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_value = usage.get("completion_tokens", usage.get("output_tokens"))
    if protocol == ModelProtocol.ANTHROPIC_MESSAGES:
        if data.get("type") == "message_start":
            usage = (data.get("message") or {}).get("usage") or usage
            input_value = usage.get("input_tokens")
        elif data.get("type") == "message_delta":
            output_value = usage.get("output_tokens")
    try:
        input_count = int(input_value) if input_value is not None else None
        output_count = int(output_value) if output_value is not None else None
    except (TypeError, ValueError):
        return None
    return input_count, output_count


def _stream_contains_tool_call(payload: str, protocol: ModelProtocol) -> bool:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if protocol == ModelProtocol.ANTHROPIC_MESSAGES:
        return (data.get("content_block") or {}).get("type") == "tool_use"
    if protocol == ModelProtocol.RESPONSES:
        return (data.get("item") or {}).get("type") in {"function_call", "computer_call"}
    for choice in data.get("choices", []):
        if (choice.get("delta") or {}).get("tool_calls"):
            return True
    return False


async def _settle(
    db: AsyncSession,
    context: ReservationContext,
    *,
    billed_usd: Decimal,
    estimated_usd: Decimal,
    used_tokens: int,
    unknown_usage: bool,
    event_type: str = "settled",
) -> None:
    reservation = (
        await db.execute(
            select(ModelReservation)
            .where(ModelReservation.id == context.reservation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    if reservation.status != "pending":
        return
    for scope, scope_id, _, _ in _scope_values(
        reservation.project_id, reservation.task_id, reservation.run_id
    ):
        counter = (
            await db.execute(
                select(ModelBudgetCounter)
                .where(
                    ModelBudgetCounter.project_id == reservation.project_id,
                    ModelBudgetCounter.scope == scope,
                    ModelBudgetCounter.scope_id == scope_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().one()
        counter.reserved_usd = max(_ZERO, counter.reserved_usd - reservation.reserved_usd)
        counter.reserved_tokens = max(0, counter.reserved_tokens - reservation.reserved_tokens)
        counter.billed_usd += billed_usd
        counter.estimated_usd += estimated_usd
        counter.used_tokens += used_tokens

    pool = (
        await db.execute(
            select(ModelPool)
            .where(
                ModelPool.organization_id == reservation.organization_id,
                ModelPool.kind == "interactive",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    deployment = (
        await db.execute(
            select(ModelDeployment)
            .where(ModelDeployment.id == reservation.deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()
    if reservation.workload == "interactive":
        pool.active_interactive = max(0, pool.active_interactive - 1)
        deployment.active_interactive = max(0, deployment.active_interactive - 1)
    else:
        pool.active_probe = max(0, pool.active_probe - 1)
        deployment.active_probe = max(0, deployment.active_probe - 1)
    reservation.status = "unknown" if unknown_usage else "settled"
    await _append_ledger(
        db,
        context,
        None,
        event_type=event_type,
        attempt=0,
        route_decision="request settled; unused reservation released",
        usage_known=not unknown_usage,
        billed_usd=billed_usd if billed_usd > _ZERO else None,
        estimated_usd=estimated_usd if estimated_usd > _ZERO else None,
        details={
            "unknown_usage": unknown_usage,
            "input_and_output_tokens": used_tokens,
            "reservation_status": reservation.status,
        },
    )


class LiteLLMModelRouter:
    """Application policy router. LiteLLM Proxy receives one selected deployment per attempt."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self.client = client
        self.client_factory = client_factory

    async def _get_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.client is not None:
            return self.client, False
        if self.client_factory is not None:
            return self.client_factory(), True
        if not settings.model_gateway_url:
            raise ModelGatewayError("model_gateway_unavailable", 503, "LiteLLM gateway URL is not configured")
        return httpx.AsyncClient(timeout=settings.model_http_timeout_seconds), True

    async def complete(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        request: ModelRouteRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if self.client is None and self.client_factory is None and not settings.model_gateway_url:
            raise ModelGatewayError("model_gateway_unavailable", 503, "LiteLLM gateway URL is not configured")
        if request.stream:
            raise ModelGatewayError("stream_endpoint_required", 400, "Use the streaming route for stream requests")
        if request.tools and not request.requirements.tools:
            raise ModelGatewayError("route_requirements_mismatch", 422, "Tool requests must require tool support")
        if request.stream and not request.requirements.streaming:
            raise ModelGatewayError("route_requirements_mismatch", 422, "Streaming requires streaming capability")
        pool, candidates = await route_candidates(
            db,
            organization_id=organization_id,
            pool_kind=request.pool.value,
            alias_name=request.model_alias,
            requirements=request.requirements,
        )
        context = await _reserve(
            db,
            organization_id=organization_id,
            project_id=project_id,
            task_id=request.task_id,
            run_id=request.run_id,
            pool=pool,
            candidates=candidates,
            request=request,
            idempotency_key=idempotency_key,
        )
        decisions: list[str] = []
        attempts = 0
        unknown_attempts = 0
        successful_usage: tuple[int | None, int | None, bool] = (None, None, False)
        successful_billed = _ZERO
        successful_estimated = _ZERO
        outcome: AttemptOutcome | None = None
        winning_candidate: RouteCandidate | None = None
        client, close_client = await self._get_client()
        try:
            base_url = settings.model_gateway_url.rstrip("/") or str(client.base_url).rstrip("/")
            for candidate_index, candidate in enumerate(context.candidates):
                if attempts >= context.max_attempts:
                    break
                if candidate_index > 0:
                    switched = await _switch_deployment(db, context, candidate)
                    if not switched:
                        decisions.append(
                            f"fallback route skipped for {candidate.deployment.name}: cooldown or deployment capacity"
                        )
                        await _append_ledger(
                            db,
                            context,
                            candidate,
                            event_type="fallback_selected",
                            attempt=attempts,
                            route_decision=decisions[-1],
                            error_code="fallback_route_unavailable",
                            details={"route_skipped": True},
                        )
                        continue
                route_local_attempts = 0
                while attempts < context.max_attempts and route_local_attempts < 2:
                    route_local_attempts += 1
                    attempts += 1
                    call_id = str(uuid4())
                    await _extend_reservation_lease(db, context.reservation.id)
                    decisions.append(
                        f"attempt {attempts}: {candidate.alias.name} -> {candidate.deployment.name}"
                    )
                    await _append_ledger(
                        db,
                        context,
                        candidate,
                        event_type="attempt_started",
                        attempt=attempts,
                        route_decision=decisions[-1],
                        request_identifier=call_id,
                    )
                    payload = request.to_provider_payload(candidate.deployment.proxy_model)
                    headers = {"X-Request-ID": call_id}
                    if request.requirements.protocol == ModelProtocol.ANTHROPIC_MESSAGES:
                        headers["anthropic-version"] = "2023-06-01"
                    started = asyncio.get_running_loop().time()
                    response: httpx.Response | None = None
                    transport_error: BaseException | None = None
                    try:
                        response = await client.post(
                            f"{base_url}{_protocol_path(request.requirements.protocol)}",
                            json=payload,
                            headers=headers,
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        transport_error = exc
                    latency_ms = max(
                        0,
                        int((asyncio.get_running_loop().time() - started) * 1000),
                    )
                    if transport_error is not None or response is None or response.status_code >= 400:
                        status = response.status_code if response is not None else None
                        error_code = _error_code(status, transport_error)
                        retry_after = _retry_after(response.headers.get("Retry-After")) if response else None
                        if response is not None:
                            await response.aclose()
                        await _record_failure(
                            db,
                            candidate,
                            error_code=error_code,
                            status_code=status,
                            retry_after=retry_after,
                        )
                        unknown_attempts += 1
                        await _append_ledger(
                            db,
                            context,
                            candidate,
                            event_type="attempt_failed",
                            attempt=attempts,
                            route_decision=decisions[-1],
                            request_identifier=call_id,
                            latency_ms=latency_ms,
                            estimated_usd=context.per_attempt_usd,
                            http_status=status,
                            error_code=error_code,
                            details={"usage_unknown": True},
                        )
                        retryable = status is None or status in {408, 429, 500, 502, 503, 504}
                        retry_wait = retry_after or 0.0
                        retry_same = (
                            retryable
                            and route_local_attempts < 2
                            and attempts < context.max_attempts
                            and retry_wait <= settings.model_max_retry_after_seconds
                        )
                        if retry_same and retry_wait > 0:
                            await asyncio.sleep(retry_wait)
                        if retry_same:
                            continue
                        if candidate_index + 1 < len(context.candidates) and attempts < context.max_attempts:
                            next_candidate = context.candidates[candidate_index + 1]
                            decisions.append(
                                f"fallback after {candidate.deployment.name} failure to {next_candidate.deployment.name}"
                            )
                            await _append_ledger(
                                db,
                                context,
                                next_candidate,
                                event_type="fallback_selected",
                                attempt=attempts,
                                route_decision=decisions[-1],
                                error_code=error_code,
                                details={"from_deployment_id": str(candidate.deployment.id)},
                            )
                        break

                    try:
                        body = response.json()
                    except ValueError:
                        await response.aclose()
                        body = None
                    if not isinstance(body, dict):
                        error_code = "provider_invalid_response"
                        await _record_failure(
                            db, candidate, error_code=error_code, status_code=response.status_code, retry_after=None
                        )
                        unknown_attempts += 1
                        await _append_ledger(
                            db,
                            context,
                            candidate,
                            event_type="attempt_failed",
                            attempt=attempts,
                            route_decision=decisions[-1],
                            request_identifier=response.headers.get("x-litellm-call-id") or call_id,
                            latency_ms=latency_ms,
                            estimated_usd=context.per_attempt_usd,
                            http_status=response.status_code,
                            error_code=error_code,
                            details={"usage_unknown": True},
                        )
                        break

                    input_tokens, output_tokens, usage_known = _extract_usage(body)
                    try:
                        billed_header = response.headers.get("x-litellm-response-cost")
                        billed = Decimal(billed_header) if billed_header is not None else None
                    except (InvalidOperation, TypeError):
                        billed = None
                    estimated = _cost_from_usage(candidate, input_tokens, output_tokens)
                    if billed is None and estimated is None:
                        estimated = context.per_attempt_usd
                        usage_known = False
                    request_identifier = (
                        response.headers.get("x-litellm-call-id")
                        or str(body.get("id") or call_id)
                    )[:200]
                    actual_model = str(body.get("model") or candidate.deployment.actual_model)[:200]
                    latency_ms = max(
                        0,
                        int((asyncio.get_running_loop().time() - started) * 1000),
                    )
                    outcome = AttemptOutcome(
                        body,
                        input_tokens,
                        output_tokens,
                        usage_known,
                        billed,
                        estimated,
                        request_identifier,
                        actual_model,
                        latency_ms,
                    )
                    winning_candidate = candidate
                    successful_usage = (input_tokens, output_tokens, usage_known)
                    successful_billed = billed or _ZERO
                    successful_estimated = estimated or _ZERO
                    await response.aclose()
                    await _record_success(db, candidate.deployment.id)
                    await _append_ledger(
                        db,
                        context,
                        candidate,
                        event_type="attempt_succeeded",
                        attempt=attempts,
                        route_decision=decisions[-1],
                        request_identifier=request_identifier,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        usage_known=usage_known,
                        latency_ms=latency_ms,
                        billed_usd=billed,
                        estimated_usd=estimated,
                        http_status=200,
                        details={"response_model": actual_model},
                    )
                    break
                if outcome is not None:
                    break

            failed_attempts = unknown_attempts
            if outcome is None:
                estimated_total = context.per_attempt_usd * failed_attempts
                estimated_tokens = context.per_attempt_tokens * failed_attempts
                await _settle(
                    db,
                    context,
                    billed_usd=_ZERO,
                    estimated_usd=estimated_total,
                    used_tokens=estimated_tokens,
                    unknown_usage=True,
                )
                raise ModelGatewayError(
                    "model_provider_unavailable", 503, "All compatible routes failed within the configured attempt bound"
                )

            input_tokens, output_tokens, usage_known = successful_usage
            used_tokens = (input_tokens or 0) + (output_tokens or 0)
            estimated_total = successful_estimated + context.per_attempt_usd * unknown_attempts
            billed_total = successful_billed
            if not usage_known:
                used_tokens += context.per_attempt_tokens
            used_tokens += context.per_attempt_tokens * unknown_attempts
            await _settle(
                db,
                context,
                billed_usd=billed_total,
                estimated_usd=estimated_total,
                used_tokens=used_tokens,
                unknown_usage=(not usage_known or unknown_attempts > 0),
            )
            assert outcome is not None
            winner = winning_candidate or context.candidates[0]
            return {
                "request_id": context.request_id,
                "model_alias": request.model_alias or winner.alias.name,
                "actual_model": winner.deployment.actual_model,
                "deployment": winner.deployment.name,
                "provider_account": winner.account.name,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "usage_known": outcome.usage_known and unknown_attempts == 0,
                "billed_usd": outcome.billed_usd,
                "estimated_usd": (outcome.estimated_usd or _ZERO) + context.per_attempt_usd * unknown_attempts,
                "route_decisions": decisions,
                "response": outcome.response or {},
            }
        finally:
            if close_client:
                await client.aclose()

    async def stream(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        request: ModelRouteRequest,
        idempotency_key: str,
    ) -> tuple[UUID, AsyncIterator[bytes]]:
        """Open a stream with pre-body failover; once any body is exposed it never retries."""
        if not request.stream or not request.requirements.streaming:
            raise ModelGatewayError("route_requirements_mismatch", 422, "Streaming must be explicitly required")
        if request.tools and not request.requirements.tools:
            raise ModelGatewayError("route_requirements_mismatch", 422, "Tool requests must require tool support")
        if self.client is None and self.client_factory is None and not settings.model_gateway_url:
            raise ModelGatewayError("model_gateway_unavailable", 503, "LiteLLM gateway URL is not configured")
        pool, candidates = await route_candidates(
            db,
            organization_id=organization_id,
            pool_kind=request.pool.value,
            alias_name=request.model_alias,
            requirements=request.requirements,
        )
        context = await _reserve(
            db,
            organization_id=organization_id,
            project_id=project_id,
            task_id=request.task_id,
            run_id=request.run_id,
            pool=pool,
            candidates=candidates,
            request=request,
            idempotency_key=idempotency_key,
        )
        client, close_client = await self._get_client()
        base_url = settings.model_gateway_url.rstrip("/") or str(client.base_url).rstrip("/")
        attempts = 0
        unknown_attempts = 0
        decisions: list[str] = []
        winner: RouteCandidate | None = None
        response: httpx.Response | None = None
        response_context: Any | None = None
        line_iterator: Any | None = None
        prelude: list[str] = []
        call_id = ""
        latency_ms = 0
        for candidate_index, candidate in enumerate(context.candidates):
            if attempts >= context.max_attempts:
                break
            if candidate_index > 0 and not await _switch_deployment(db, context, candidate):
                decisions.append(f"fallback route skipped for {candidate.deployment.name}: cooldown or capacity")
                await _append_ledger(
                    db, context, candidate, event_type="fallback_selected", attempt=attempts,
                    route_decision=decisions[-1], error_code="fallback_route_unavailable",
                    details={"route_skipped": True},
                )
                continue
            route_local_attempts = 0
            while attempts < context.max_attempts and route_local_attempts < 2:
                route_local_attempts += 1
                attempts += 1
                call_id = str(uuid4())
                await _extend_reservation_lease(db, context.reservation.id)
                decisions.append(f"attempt {attempts}: {candidate.alias.name} -> {candidate.deployment.name}")
                await _append_ledger(
                    db, context, candidate, event_type="attempt_started", attempt=attempts,
                    route_decision=decisions[-1], request_identifier=call_id,
                )
                headers = {"X-Request-ID": call_id}
                if request.requirements.protocol == ModelProtocol.ANTHROPIC_MESSAGES:
                    headers["anthropic-version"] = "2023-06-01"
                started = asyncio.get_running_loop().time()
                response_context = client.stream(
                    "POST",
                    f"{base_url}{_protocol_path(request.requirements.protocol)}",
                    json=request.to_provider_payload(candidate.deployment.proxy_model),
                    headers=headers,
                )
                try:
                    response = await response_context.__aenter__()
                    latency_ms = max(0, int((asyncio.get_running_loop().time() - started) * 1000))
                    if response.status_code >= 400:
                        status = response.status_code
                        retry_after = _retry_after(response.headers.get("Retry-After"))
                        await response.aclose()
                        await response_context.__aexit__(None, None, None)
                        response_context = None
                        response = None
                        error_code = _error_code(status)
                        await _record_failure(
                            db, candidate, error_code=error_code, status_code=status, retry_after=retry_after
                        )
                        unknown_attempts += 1
                        await _append_ledger(
                            db, context, candidate, event_type="attempt_failed", attempt=attempts,
                            route_decision=decisions[-1], request_identifier=call_id,
                            latency_ms=latency_ms, estimated_usd=context.per_attempt_usd,
                            http_status=status, error_code=error_code, details={"usage_unknown": True},
                        )
                        retryable = status in {408, 429, 500, 502, 503, 504}
                        wait_seconds = retry_after or 0.0
                        if (
                            retryable and route_local_attempts < 2 and attempts < context.max_attempts
                            and wait_seconds <= settings.model_max_retry_after_seconds
                        ):
                            if wait_seconds:
                                await asyncio.sleep(wait_seconds)
                            continue
                        if candidate_index + 1 < len(context.candidates) and attempts < context.max_attempts:
                            next_candidate = context.candidates[candidate_index + 1]
                            await _append_ledger(
                                db, context, next_candidate, event_type="fallback_selected", attempt=attempts,
                                route_decision=f"fallback after {candidate.deployment.name} failure to {next_candidate.deployment.name}",
                                error_code=error_code,
                                details={"from_deployment_id": str(candidate.deployment.id)},
                            )
                        break
                    line_iterator = response.aiter_lines().__aiter__()
                    first_line = await line_iterator.__anext__()
                    prelude = [first_line]
                    winner = candidate
                    break
                except (httpx.TimeoutException, httpx.TransportError, StopAsyncIteration) as exc:
                    if response_context is not None:
                        await response_context.__aexit__(type(exc), exc, exc.__traceback__)
                    response_context = None
                    response = None
                    error_code = "provider_timeout" if isinstance(exc, httpx.TimeoutException) else "provider_transport_error"
                    await _record_failure(db, candidate, error_code=error_code, status_code=None, retry_after=None)
                    unknown_attempts += 1
                    await _append_ledger(
                        db, context, candidate, event_type="attempt_failed", attempt=attempts,
                        route_decision=decisions[-1], request_identifier=call_id,
                        latency_ms=latency_ms, estimated_usd=context.per_attempt_usd,
                        error_code=error_code, details={"usage_unknown": True, "before_first_chunk": True},
                    )
                    if route_local_attempts < 2 and attempts < context.max_attempts:
                        continue
                    if candidate_index + 1 < len(context.candidates) and attempts < context.max_attempts:
                        next_candidate = context.candidates[candidate_index + 1]
                        await _append_ledger(
                            db, context, next_candidate, event_type="fallback_selected", attempt=attempts,
                            route_decision=f"fallback after {candidate.deployment.name} transport failure to {next_candidate.deployment.name}",
                            error_code=error_code,
                            details={"from_deployment_id": str(candidate.deployment.id)},
                        )
                    break
                break
            if winner is not None:
                break

        if winner is None or response is None or response_context is None or line_iterator is None:
            if close_client:
                await client.aclose()
            await _settle(
                db,
                context,
                billed_usd=_ZERO,
                estimated_usd=context.per_attempt_usd * unknown_attempts,
                used_tokens=context.per_attempt_tokens * unknown_attempts,
                unknown_usage=True,
            )
            raise ModelGatewayError(
                "model_provider_unavailable", 503, "All compatible routes failed before the first stream chunk"
            )

        heartbeat_task = asyncio.create_task(
            _reservation_heartbeat(context.request_id, context.policy.max_wall_seconds)
        )

        async def body_stream() -> AsyncIterator[bytes]:
            input_tokens: int | None = None
            output_tokens: int | None = None
            usage_seen = False
            emitted = False
            tool_call_chunks = False
            stream_error: BaseException | None = None
            try:
                for line in prelude:
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        usage = _stream_usage(payload, request.requirements.protocol)
                        if usage is not None:
                            input_tokens = usage[0] if usage[0] is not None else input_tokens
                            output_tokens = usage[1] if usage[1] is not None else output_tokens
                            usage_seen = input_tokens is not None and output_tokens is not None
                        tool_call_chunks = tool_call_chunks or _stream_contains_tool_call(
                            payload, request.requirements.protocol
                        )
                    emitted = True
                    yield (line + "\n").encode()
                async for line in line_iterator:
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        usage = _stream_usage(payload, request.requirements.protocol)
                        if usage is not None:
                            input_tokens = usage[0] if usage[0] is not None else input_tokens
                            output_tokens = usage[1] if usage[1] is not None else output_tokens
                            usage_seen = input_tokens is not None and output_tokens is not None
                        tool_call_chunks = tool_call_chunks or _stream_contains_tool_call(
                            payload, request.requirements.protocol
                        )
                    emitted = True
                    yield (line + "\n").encode()
                billed_header = response.headers.get("x-litellm-response-cost")
                try:
                    billed = Decimal(billed_header) if billed_header is not None else _ZERO
                except (InvalidOperation, TypeError):
                    billed = _ZERO
                estimated = _cost_from_usage(winner, input_tokens, output_tokens) if usage_seen else None
                unknown = not usage_seen
                if unknown and billed == _ZERO:
                    estimated = context.per_attempt_usd
                actual_tokens = (input_tokens or 0) + (output_tokens or 0)
                await _record_success(db, winner.deployment.id)
                await _append_ledger(
                    db, context, winner, event_type="attempt_succeeded", attempt=attempts,
                    route_decision=decisions[-1],
                    request_identifier=response.headers.get("x-litellm-call-id") or call_id,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    usage_known=usage_seen, latency_ms=latency_ms,
                    billed_usd=billed if billed > _ZERO else None,
                    estimated_usd=estimated,
                    http_status=200,
                    details={"stream_complete": True},
                )
                await _settle(
                    db, context,
                    billed_usd=billed,
                    estimated_usd=(estimated or _ZERO) + context.per_attempt_usd * unknown_attempts,
                    used_tokens=actual_tokens + context.per_attempt_tokens * unknown_attempts,
                    unknown_usage=unknown or unknown_attempts > 0,
                )
            except asyncio.CancelledError as exc:
                stream_error = exc
                await asyncio.shield(
                    _append_ledger(
                        db, context, winner, event_type="attempt_failed", attempt=attempts,
                        route_decision="stream consumer disconnected; no workflow or tool effect was replayed",
                        request_identifier=response.headers.get("x-litellm-call-id") or call_id,
                        input_tokens=input_tokens, output_tokens=output_tokens, usage_known=False,
                        latency_ms=latency_ms, estimated_usd=context.per_attempt_usd,
                        error_code="stream_disconnected",
                        details={"partial_stream": emitted, "tool_call_chunks_emitted": tool_call_chunks},
                    )
                )
                await asyncio.shield(
                    _settle(
                        db, context, billed_usd=_ZERO,
                        estimated_usd=context.per_attempt_usd * (unknown_attempts + 1),
                        used_tokens=context.per_attempt_tokens * (unknown_attempts + 1),
                        unknown_usage=True,
                    )
                )
                raise
            except Exception as exc:  # noqa: BLE001 - an HTTP stream may fail after partial output.
                stream_error = exc
                error_code = "partial_stream_failure" if emitted else "provider_stream_failure"
                await _append_ledger(
                    db, context, winner, event_type="attempt_failed", attempt=attempts,
                    route_decision="stream terminated after response began; transport retry suppressed",
                    request_identifier=response.headers.get("x-litellm-call-id") or call_id,
                    input_tokens=input_tokens, output_tokens=output_tokens, usage_known=False,
                    latency_ms=latency_ms, estimated_usd=context.per_attempt_usd,
                    error_code=error_code,
                    details={
                        "partial_stream": emitted,
                        "tool_call_chunks_emitted": tool_call_chunks,
                        "workflow_replayed": False,
                    },
                )
                await _settle(
                    db, context, billed_usd=_ZERO,
                    estimated_usd=context.per_attempt_usd * (unknown_attempts + 1),
                    used_tokens=context.per_attempt_tokens * (unknown_attempts + 1),
                    unknown_usage=True,
                )
                if emitted:
                    yield (
                        'event: model.error\ndata: '
                        + json.dumps({
                            "code": error_code,
                            "message": "The provider stream ended early; no agent workflow or tool side effect was replayed.",
                            "tool_call_chunks_emitted": tool_call_chunks,
                        }, separators=(",", ":"))
                        + "\n\n"
                    ).encode()
            finally:
                await _stop_heartbeat(heartbeat_task)
                await response_context.__aexit__(
                    type(stream_error) if stream_error else None,
                    stream_error,
                    stream_error.__traceback__ if stream_error else None,
                )
                if close_client:
                    await client.aclose()

        return context.request_id, body_stream()


async def reconcile_stale_model_reservations(
    session_factory: async_sessionmaker[AsyncSession], *, limit: int = 100
) -> int:
    """Conservatively settle expired leases as estimated spend and release all capacity."""
    reconciled = 0
    async with session_factory() as db:
        stale = (
            await db.execute(
                select(ModelReservation)
                .where(
                    ModelReservation.status == "pending",
                    ModelReservation.lease_expires_at <= datetime.now(UTC),
                )
                .order_by(ModelReservation.lease_expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for reservation in stale:
            counters = (
                await db.execute(
                    select(ModelBudgetCounter)
                    .where(ModelBudgetCounter.project_id == reservation.project_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            for counter in counters:
                if counter.scope_id in {reservation.project_id, reservation.task_id, reservation.run_id}:
                    counter.reserved_usd = max(_ZERO, counter.reserved_usd - reservation.reserved_usd)
                    counter.reserved_tokens = max(0, counter.reserved_tokens - reservation.reserved_tokens)
                    counter.estimated_usd += reservation.reserved_usd
                    counter.used_tokens += reservation.reserved_tokens
            capacity = (
                await db.execute(
                    select(ModelPool)
                    .where(
                        ModelPool.organization_id == reservation.organization_id,
                        ModelPool.kind == "interactive",
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
            deployment = (
                await db.execute(
                    select(ModelDeployment)
                    .where(ModelDeployment.id == reservation.deployment_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().first()
            if capacity is not None:
                if reservation.workload == "interactive":
                    capacity.active_interactive = max(0, capacity.active_interactive - 1)
                else:
                    capacity.active_probe = max(0, capacity.active_probe - 1)
            if deployment is not None:
                if reservation.workload == "interactive":
                    deployment.active_interactive = max(0, deployment.active_interactive - 1)
                else:
                    deployment.active_probe = max(0, deployment.active_probe - 1)
            reservation.status = "unknown"
            db.add(
                ModelRequestLedger(
                    request_id=reservation.request_id,
                    attempt=0,
                    event_type="stale_reconciled",
                    organization_id=reservation.organization_id,
                    project_id=reservation.project_id,
                    task_id=reservation.task_id,
                    run_id=reservation.run_id,
                    pool_name="reconciled",
                    model_alias="unknown",
                    route_decision="stale request reservation charged conservatively; provider usage unknown",
                    usage_known=False,
                    estimated_usd=reservation.reserved_usd,
                    details={"reservation_status": "unknown", "reserved_tokens": reservation.reserved_tokens},
                )
            )
            reconciled += 1
        await db.commit()
    return reconciled
