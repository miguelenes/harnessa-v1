"""Grouped, immutable evaluation manifests over bounded Harness² probe runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.harness2 import UPSTREAM_REVISION
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import TaskStage
from harnessa.db.harness import EvaluationRun, HarnessExperience, HarnessExperiment, HarnessPolicy
from harnessa.db.model_gateway import (
    AliasDeployment,
    ModelAlias,
    ModelBudgetCounter,
    ModelBudgetPolicy,
    ModelDeployment,
    ModelPool,
    PoolAlias,
)
from harnessa.db.models import Project, Repository, Task, User
from harnessa.db.session import get_db
from harnessa.db.specification import SpecificationInterview
from harnessa.domain.skill_evolution import scopes_overlap, validate_skill_artifact
from harnessa.services.authorization import require_membership
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["evaluations"])
MAX_EVALUATION_TASKS = 20


class EvaluationRunCreate(BaseModel):
    stable_key: str = Field(min_length=1, max_length=160)
    suite: str = Field(pattern=r"^harness2-local$")
    task_ids: list[UUID] = Field(min_length=1, max_length=MAX_EVALUATION_TASKS)
    mode: str = Field(pattern=r"^(parallel|sequential|both)$")
    k: int = Field(ge=1, le=3)
    route_alias: str = Field(min_length=1, max_length=120)
    skill_experience_id: UUID | None = None


class EvaluationRunRead(BaseModel):
    id: UUID
    stable_key: str
    status: str
    manifest: dict[str, Any]
    manifest_digest: str
    sample_size: int
    completed_samples: int
    failed_samples: int
    cancelled_samples: int
    unknown_samples: int
    missing_samples: int
    delivery_samples: int
    delivery_successes: int
    delivery_wilson_95: list[float] | None
    cost_usd: str | None
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    experiment_ids: list[str]
    created_at: datetime


class EvaluationDryRun(BaseModel):
    suite: str
    manifest: dict[str, Any]
    manifest_digest: str
    task_count: int
    cost_estimate_usd: str
    estimate_kind: str
    within_project_policy: bool


async def _operator(db: AsyncSession, actor: User, project_id: UUID) -> Project:
    membership = await require_membership(db, actor, project_id)
    if getattr(membership, "role", "") not in {"administrator", "operator"}:
        raise HTTPException(403, "Evaluation requires an authorized project operator")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _digest(manifest: dict[str, Any]) -> str:
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


async def _frozen_inputs(
    db: AsyncSession, project_id: UUID, task_ids: list[UUID]
) -> tuple[list[dict[str, Any]], int]:
    if len(set(task_ids)) != len(task_ids):
        raise HTTPException(422, "Evaluation task IDs must be unique")
    policy = await db.scalar(select(HarnessPolicy).where(
        HarnessPolicy.project_id == project_id
    ).with_for_update())
    if policy is None:
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        policy = HarnessPolicy(organization_id=project.organization_id, project_id=project_id)
        db.add(policy)
        await db.flush()
    frozen: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = await db.scalar(select(Task).where(
            Task.id == task_id, Task.project_id == project_id
        ).with_for_update())
        if task is None:
            raise HTTPException(404, "One or more evaluation tasks are outside this project")
        if task.stage != TaskStage.HARNESS_PROBE.value:
            raise HTTPException(409, "Every local evaluation task must be in HARNESS_PROBE")
        interview = await db.scalar(
            select(SpecificationInterview).where(
                SpecificationInterview.task_id == task_id
            ).with_for_update()
        )
        if interview is None or interview.readiness_state != "READY":
            raise HTTPException(409, "Every evaluation task needs a ready frozen specification")
        repository = await db.get(Repository, interview.repository_id)
        if repository is None:
            raise HTTPException(409, "Every evaluation task needs an authorized repository")
        frozen.append({
            "task_id": str(task.id),
            "title_digest": hashlib.sha256(task.title.encode()).hexdigest(),
            "base_sha": interview.base_sha,
            "specification_hash": interview.artifact_hash,
            "specification_revision": interview.revision,
            "policy_revision": policy.revision,
            "repository_scope": repository.target_id,
        })
    return frozen, policy.revision


async def _frozen_skill_version(
    db: AsyncSession,
    project_id: UUID,
    experience_id: UUID | None,
    task_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if experience_id is None:
        return []
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    experience = await db.scalar(
        select(HarnessExperience).where(
            HarnessExperience.id == experience_id,
            or_(
                HarnessExperience.project_id == project_id,
                (HarnessExperience.organization_id == project.organization_id)
                & (HarnessExperience.scope_type == "organization")
                & (HarnessExperience.scope_id == str(project.organization_id)),
            ),
        ).with_for_update()
    )
    if (
        experience is None
        or experience.status != "candidate"
        or experience.expiry_at <= datetime.now(UTC)
        or experience.conflict_state != "clear"
        or not experience.artifact_digest
        or not experience.artifact_storage_key
        or not experience.artifact_path
        or not experience.artifact_size_bytes
    ):
        raise HTTPException(409, "Evaluation requires one unexpired, non-conflicting skill candidate")
    competing_candidates = list(await db.scalars(select(HarnessExperience).where(
        HarnessExperience.organization_id == project.organization_id,
        HarnessExperience.conflict_key == experience.conflict_key,
        HarnessExperience.status == "candidate",
        HarnessExperience.id != experience.id,
        or_(
            HarnessExperience.project_id == project_id,
            HarnessExperience.scope_type == "organization",
        ),
    )))
    if any(
        item.artifact_digest != experience.artifact_digest
        and scopes_overlap(
            experience.scope_type,
            experience.scope_id,
            experience.repository_scope,
            item.scope_type,
            item.scope_id,
            item.repository_scope,
        )
        for item in competing_candidates
    ):
        raise HTTPException(409, "Conflicting skill candidates cannot enter evaluation")
    repositories = {item["repository_scope"] for item in task_inputs}
    if experience.scope_type == "repository" and repositories != {experience.scope_id}:
        raise HTTPException(409, "Every evaluation task must match the skill's repository scope")
    if experience.scope_type == "project" and experience.scope_id != str(project_id):
        raise HTTPException(409, "Skill project scope does not match the evaluation project")
    if experience.scope_type == "task" and {item["task_id"] for item in task_inputs} != {experience.scope_id}:
        raise HTTPException(409, "Task-scoped skills require an evaluation of their exact task only")
    if experience.scope_type == "organization" and experience.scope_id != str(project.organization_id):
        raise HTTPException(409, "Skill organization scope does not match the evaluation project")
    store = LocalArtifactStore(Path(settings.artifact_root))
    state = store.verify_reference(
        experience.artifact_storage_key,
        expected_digest=experience.artifact_digest,
        expected_size=experience.artifact_size_bytes,
        max_bytes=64_000,
    )
    if state != "verified":
        raise HTTPException(409, "Skill artifact CAS reference failed verification")
    content = (store.root / experience.artifact_storage_key).read_text(encoding="utf-8")
    try:
        artifact = validate_skill_artifact(experience.artifact_path, content)
    except ValueError as exc:
        raise HTTPException(409, "Skill artifact schema is invalid") from exc
    if artifact.digest != experience.artifact_digest:
        raise HTTPException(409, "Skill artifact changed after it was recorded")
    return [{
        "id": str(experience.id),
        "experience_key": experience.experience_key,
        "version": experience.version,
        "candidate_digest": experience.candidate_digest,
        "artifact_digest": experience.artifact_digest,
        "artifact_path": experience.artifact_path,
        "artifact_storage_key": experience.artifact_storage_key,
        "artifact_size_bytes": experience.artifact_size_bytes,
        "source_kind": experience.source_kind,
        "scope_type": experience.scope_type,
        "scope_id": experience.scope_id,
        "license_id": experience.license_id,
    }]


async def _route_manifest(
    db: AsyncSession, project_id: UUID, route_alias: str
) -> dict[str, Any]:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    alias = await db.scalar(select(ModelAlias).where(
        ModelAlias.organization_id == project.organization_id,
        ModelAlias.name == route_alias,
        ModelAlias.enabled.is_(True),
    ))
    if alias is None:
        raise HTTPException(409, "The selected model alias is not enabled")
    deployments = list(await db.execute(
        select(ModelDeployment, AliasDeployment.priority)
        .join(AliasDeployment, AliasDeployment.deployment_id == ModelDeployment.id)
        .where(AliasDeployment.alias_id == alias.id, ModelDeployment.enabled.is_(True))
        .order_by(AliasDeployment.priority)
    ))
    return {
        "alias": route_alias,
        "deployments": [
            {
                "id": str(deployment.id),
                "proxy_model": deployment.proxy_model,
                "actual_model": deployment.actual_model,
                "priority": priority,
                "price_revision": deployment.price_revision,
                "capabilities": deployment.capabilities,
                "context_window": deployment.context_window,
                "input_cost_per_million": str(deployment.input_cost_per_million),
                "output_cost_per_million": str(deployment.output_cost_per_million),
            }
            for deployment, priority in deployments
        ],
    }


def _evaluation_manifest(
    *,
    suite: str,
    task_inputs: list[dict[str, Any]],
    mode: str,
    k: int,
    route_alias: str,
    route: dict[str, Any],
    skill_versions: list[dict[str, Any]],
    policy_revision: int,
    estimate: Decimal,
    budget: ModelBudgetPolicy,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "suite": suite,
        "suite_revision": UPSTREAM_REVISION,
        "task_inputs": task_inputs,
        "task_subset_seed": None,
        "common_base": sorted({item["base_sha"] for item in task_inputs}),
        "candidate_diff": {"skill_versions": skill_versions},
        "mode": mode,
        "k": k,
        "model_route_policy": route,
        "route_alias": route_alias,
        "harness_policy_revision": policy_revision,
        "executor": {
            "name": "opencode-acp",
            "image": settings.acp_agent_image,
            "tool_revision": settings.acp_harness_revision,
            "mcp_grants_revision": None,
        },
        "harness_revision": UPSTREAM_REVISION,
        "skill_versions": skill_versions,
        "benchmark": {"revision": UPSTREAM_REVISION, "judge_revision": None},
        "budget": {
            "max_cost_usd": str(estimate),
            "max_output_tokens": budget.max_output_tokens,
            "max_steps": budget.max_steps,
            "max_attempts": budget.max_attempts,
            "project_limit_usd": str(budget.probe_project_limit_usd),
            "task_limit_usd": str(budget.probe_task_limit_usd),
            "run_limit_usd": str(budget.probe_run_limit_usd),
        },
        "environment": {"image_digest": settings.acp_agent_image},
        "evidence_artifacts": [],
        "max_tasks": MAX_EVALUATION_TASKS,
        "estimate_usd": str(estimate),
    }


async def _estimate_cost(
    db: AsyncSession, project_id: UUID, route_alias: str, *, task_ids: list[UUID], mode: str, k: int
) -> tuple[Decimal, ModelBudgetPolicy]:
    budget = await db.scalar(select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id))
    if budget is None:
        raise HTTPException(409, "Configure a finite project/task/run model budget before evaluation")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    alias = await db.scalar(select(ModelAlias).where(
        ModelAlias.organization_id == project.organization_id,
        ModelAlias.name == route_alias,
        ModelAlias.enabled.is_(True),
    ))
    if alias is None:
        raise HTTPException(409, "The selected model alias is not enabled")
    probe_pool = await db.scalar(select(ModelPool).where(
        ModelPool.organization_id == project.organization_id,
        ModelPool.kind == "probe",
    ))
    if probe_pool is None or await db.scalar(select(PoolAlias.id).where(
        PoolAlias.pool_id == probe_pool.id, PoolAlias.alias_id == alias.id
    )) is None:
        raise HTTPException(409, "The selected model alias is not configured in the probe pool")
    deployments = list(await db.scalars(
        select(ModelDeployment)
        .join(AliasDeployment, AliasDeployment.deployment_id == ModelDeployment.id)
        .where(AliasDeployment.alias_id == alias.id, ModelDeployment.enabled.is_(True))
        .order_by(AliasDeployment.priority)
    ))
    if not deployments or any(
        row.input_cost_per_million is None or row.output_cost_per_million is None
        or row.context_window <= budget.max_output_tokens
        for row in deployments
    ):
        raise HTTPException(409, "The selected alias lacks complete pricing or context capability for a bound estimate")
    worst_per_attempt = max(
        (Decimal(row.input_cost_per_million) * (row.context_window - budget.max_output_tokens)
         + Decimal(row.output_cost_per_million) * budget.max_output_tokens)
        / Decimal(1_000_000)
        for row in deployments
    )
    # Budget every bounded model step in each baseline/candidate/composed rollout, including
    # worst-case transport attempts. Without max_steps the displayed ceiling understates work.
    lane_count = 2 if mode == "both" else 1
    rollout_count = lane_count * (k + 2)
    per_task_estimate = worst_per_attempt * Decimal(rollout_count * budget.max_steps * budget.max_attempts)
    estimate = per_task_estimate * len(task_ids)
    harness_policy = await db.scalar(select(HarnessPolicy).where(
        HarnessPolicy.project_id == project_id
    ))
    if harness_policy is None:
        raise HTTPException(409, "Configure a Harness² evaluation policy before evaluation")
    estimate *= Decimal(harness_policy.cost_multiplier)
    per_task_estimate *= Decimal(harness_policy.cost_multiplier)
    if per_task_estimate > budget.probe_task_limit_usd:
        raise HTTPException(409, "Worst-case evaluation estimate exceeds the configured probe task budget")
    if estimate > budget.probe_run_limit_usd:
        raise HTTPException(409, "Worst-case evaluation estimate exceeds the configured probe run budget")
    project_counter = await db.scalar(select(ModelBudgetCounter).where(
        ModelBudgetCounter.project_id == project_id,
        ModelBudgetCounter.workload == "probe",
        ModelBudgetCounter.scope == "project",
        ModelBudgetCounter.scope_id == project_id,
    ))
    consumed = (
        project_counter.billed_usd + project_counter.estimated_usd + project_counter.reserved_usd
        if project_counter is not None else Decimal(0)
    )
    if consumed + estimate > budget.probe_project_limit_usd:
        raise HTTPException(409, "Worst-case evaluation estimate exceeds the remaining probe project budget")
    for task_id in task_ids:
        task_counter = await db.scalar(select(ModelBudgetCounter).where(
            ModelBudgetCounter.project_id == project_id,
            ModelBudgetCounter.workload == "probe",
            ModelBudgetCounter.scope == "task",
            ModelBudgetCounter.scope_id == task_id,
        ))
        task_consumed = (
            task_counter.billed_usd + task_counter.estimated_usd + task_counter.reserved_usd
            if task_counter is not None else Decimal(0)
        )
        if task_consumed + per_task_estimate > budget.probe_task_limit_usd:
            raise HTTPException(409, "Worst-case evaluation estimate exceeds a task's remaining probe budget")
    return estimate.quantize(Decimal("0.00000001")), budget


@router.get("/projects/{project_id}/evaluations/catalog")
async def evaluation_catalog(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_membership(db, actor, project_id)
    return {"suites": [
        {"id": "harness2-local", "available": True, "kind": "local", "requires": "READY HARNESS_PROBE tasks, a Harness² policy, a probe route and finite probe budget"},
        {"id": "workbuddy-bench", "available": False, "state": "blocked", "reason": "License restricts use in the operator's EU jurisdiction; no dataset is fetched."},
        {"id": "jobbench", "available": False, "state": "unavailable", "reason": "Isolated runner and budget-metered judge adapter are not configured."},
        {"id": "harvey-lab", "available": False, "state": "unavailable", "reason": "Isolated runner and budget-metered judge adapter are not configured."},
    ]}


@router.post("/projects/{project_id}/evaluations/dry-run", response_model=EvaluationDryRun)
async def evaluation_dry_run(
    project_id: UUID,
    body: EvaluationRunCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EvaluationDryRun:
    await _operator(db, actor, project_id)
    tasks, policy_revision = await _frozen_inputs(db, project_id, body.task_ids)
    harness_policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    if harness_policy is None or body.k > harness_policy.max_k:
        raise HTTPException(422, "Evaluation exceeds the configured Harness² sample policy")
    estimate, budget = await _estimate_cost(
        db, project_id, body.route_alias, task_ids=body.task_ids, mode=body.mode, k=body.k
    )
    skills = await _frozen_skill_version(
        db, project_id, body.skill_experience_id, tasks
    )
    manifest = _evaluation_manifest(
        suite=body.suite,
        task_inputs=tasks,
        mode=body.mode,
        k=body.k,
        route_alias=body.route_alias,
        route=await _route_manifest(db, project_id, body.route_alias),
        skill_versions=skills,
        policy_revision=policy_revision,
        estimate=estimate,
        budget=budget,
    )
    return EvaluationDryRun(
        suite=body.suite, manifest=manifest, manifest_digest=_digest(manifest),
        task_count=len(tasks), cost_estimate_usd=str(estimate),
        estimate_kind="conservative_context_window_upper_bound",
        within_project_policy=True,
    )


@router.post("/projects/{project_id}/evaluations", response_model=EvaluationRunRead, status_code=202)
async def create_evaluation_run(
    project_id: UUID,
    body: EvaluationRunCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EvaluationRunRead:
    project = await _operator(db, actor, project_id)
    tasks, policy_revision = await _frozen_inputs(db, project_id, body.task_ids)
    harness_policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    if harness_policy is None or body.k > harness_policy.max_k:
        raise HTTPException(422, "Evaluation exceeds the configured Harness² sample policy")
    estimate, budget = await _estimate_cost(
        db, project_id, body.route_alias, task_ids=body.task_ids, mode=body.mode, k=body.k
    )
    skills = await _frozen_skill_version(
        db, project_id, body.skill_experience_id, tasks
    )
    manifest = _evaluation_manifest(
        suite=body.suite,
        task_inputs=tasks,
        mode=body.mode,
        k=body.k,
        route_alias=body.route_alias,
        route=await _route_manifest(db, project_id, body.route_alias),
        skill_versions=skills,
        policy_revision=policy_revision,
        estimate=estimate,
        budget=budget,
    )
    digest = _digest(manifest)
    prior = await db.scalar(select(EvaluationRun).where(
        EvaluationRun.project_id == project_id, EvaluationRun.stable_key == body.stable_key
    ).with_for_update())
    if prior:
        if prior.manifest_digest != digest:
            raise HTTPException(409, "Evaluation key is already bound to a different immutable manifest")
        return await _read_run(db, prior)
    run = EvaluationRun(
        id=uuid4(),
        organization_id=project.organization_id, project_id=project_id, created_by=actor.id,
        stable_key=body.stable_key, status="queued", manifest=manifest,
        manifest_digest=digest, experiment_ids=[], max_cost_usd=estimate,
    )
    db.add(run)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        prior = await db.scalar(select(EvaluationRun).where(
            EvaluationRun.project_id == project_id,
            EvaluationRun.stable_key == body.stable_key,
        ))
        if prior is not None and prior.manifest_digest == digest:
            return await _read_run(db, prior)
        raise HTTPException(409, "Evaluation idempotency key conflicts with an existing manifest") from exc
    experiment_ids: list[str] = []
    for task in tasks:
        task_id = UUID(task["task_id"])
        interview = await db.scalar(select(SpecificationInterview).where(
            SpecificationInterview.task_id == task_id
        ))
        child_key = hashlib.sha256(f"{body.stable_key}:{task_id}".encode()).hexdigest()
        child = HarnessExperiment(
            organization_id=project.organization_id, project_id=project_id, task_id=task_id,
            evaluation_run_id=run.id,
            created_by=actor.id, stable_key=f"eval-{child_key}", mode=body.mode, k=body.k,
            status="queued", phase="queued", attempts=0, base_sha=task["base_sha"],
            specification_hash=task["specification_hash"],
            specification_revision=task["specification_revision"],
            policy_revision=policy_revision, harness2_revision=UPSTREAM_REVISION,
            result={
                "route_alias": body.route_alias,
                "evaluation_manifest_digest": digest,
                "skill_versions": skills,
            },
        )
        if interview is None:
            raise HTTPException(409, "A frozen evaluation task changed during manifest creation")
        db.add(child)
        await db.flush()
        experiment_ids.append(str(child.id))
    run.experiment_ids = experiment_ids
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        prior = await db.scalar(select(EvaluationRun).where(
            EvaluationRun.project_id == project_id,
            EvaluationRun.stable_key == body.stable_key,
        ))
        if prior is not None and prior.manifest_digest == digest:
            return await _read_run(db, prior)
        raise HTTPException(409, "Evaluation idempotency key conflicts with an existing manifest") from exc
    await db.refresh(run)
    return await _read_run(db, run)


@router.get("/projects/{project_id}/evaluations")
async def list_evaluation_runs(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await require_membership(db, actor, project_id)
    rows = list(await db.scalars(select(EvaluationRun).where(
        EvaluationRun.project_id == project_id
    ).order_by(EvaluationRun.created_at.desc()).limit(100)))
    return {"runs": [await _read_run(db, row) for row in rows]}


@router.post("/projects/{project_id}/evaluations/{evaluation_id}/cancel")
async def cancel_evaluation_run(
    project_id: UUID, evaluation_id: UUID,
    actor: User = Depends(current_active_user), db: AsyncSession = Depends(get_db),
) -> EvaluationRunRead:
    await _operator(db, actor, project_id)
    run = await db.scalar(select(EvaluationRun).where(
        EvaluationRun.project_id == project_id, EvaluationRun.id == evaluation_id
    ).with_for_update())
    if run is None:
        raise HTTPException(404, "Evaluation run not found")
    if run.status not in {"succeeded", "failed", "cancelled", "blocked"}:
        now = datetime.now(UTC)
        run.cancel_requested_at = now
        experiments = list(await db.scalars(select(HarnessExperiment).where(
            HarnessExperiment.id.in_([UUID(value) for value in run.experiment_ids])
        ).with_for_update()))
        for child in experiments:
            if child.status == "queued":
                child.status, child.phase, child.completed_at = "cancelled", "cancelled", now
            elif child.status == "running":
                child.cancel_requested_at = now
        await db.commit()
    return await _read_run(db, run)


@router.post("/projects/{project_id}/evaluations/{evaluation_id}/resume", status_code=202)
async def resume_evaluation_run(
    project_id: UUID, evaluation_id: UUID,
    actor: User = Depends(current_active_user), db: AsyncSession = Depends(get_db),
) -> EvaluationRunRead:
    """Resume failed children only when every frozen input and policy still matches."""
    await _operator(db, actor, project_id)
    run = await db.scalar(select(EvaluationRun).where(
        EvaluationRun.project_id == project_id, EvaluationRun.id == evaluation_id
    ).with_for_update())
    if run is None:
        raise HTTPException(404, "Evaluation run not found")
    if run.cancel_requested_at is not None:
        raise HTTPException(409, "A cancelled evaluation cannot be resumed")
    # The API derives terminal status from child experiments. The stored parent
    # row can still say "queued" after all children have failed, so use that
    # authoritative summary before deciding whether a resume is valid.
    summary = await _read_run(db, run)
    if summary.status not in {"failed", "blocked"}:
        raise HTTPException(409, "Only failed evaluation runs can be resumed")
    policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    children = list(await db.scalars(select(HarnessExperiment).where(
        HarnessExperiment.id.in_([UUID(value) for value in run.experiment_ids])
    ).with_for_update()))
    manifest_tasks = {str(item["task_id"]): item for item in run.manifest["task_inputs"]}
    resumed = 0
    for child in children:
        if child.status not in {"failed", "blocked"}:
            continue
        task = await db.scalar(select(Task).where(
            Task.id == child.task_id, Task.project_id == project_id
        ).with_for_update())
        interview = await db.scalar(select(SpecificationInterview).where(
            SpecificationInterview.task_id == child.task_id
        ).with_for_update())
        frozen = manifest_tasks.get(str(child.task_id), {})
        resume_count = (child.result or {}).get("resume_count", 0)
        if (
            not isinstance(resume_count, int) or isinstance(resume_count, bool)
            or resume_count >= 2 or task is None
            or task.stage != TaskStage.HARNESS_PROBE.value
            or interview is None or interview.readiness_state != "READY"
            or interview.base_sha != frozen.get("base_sha")
            or interview.artifact_hash != frozen.get("specification_hash")
            or interview.revision != frozen.get("specification_revision")
            or policy is None or policy.revision != frozen.get("policy_revision")
        ):
            raise HTTPException(409, "A failed child input or policy changed; create a new evaluation manifest")
        child.result = {**(child.result or {}), "resume_count": resume_count + 1}
        child.status, child.phase, child.last_error = "queued", "queued", None
        child.worker_heartbeat_at = None
        resumed += 1
    if resumed == 0:
        raise HTTPException(409, "No failed child experiment is eligible to resume")
    run.status, run.completed_at = "queued", None
    await db.commit()
    return await _read_run(db, run)


async def _read_run(db: AsyncSession, run: EvaluationRun) -> EvaluationRunRead:
    experiments = list(await db.scalars(select(HarnessExperiment).where(
        HarnessExperiment.id.in_([UUID(value) for value in run.experiment_ids])
    )))
    states = [row.status for row in experiments]
    if run.cancel_requested_at is not None and all(state in {"cancelled", "failed", "blocked", "succeeded"} for state in states):
        status = "cancelled"
    elif states and all(state == "succeeded" for state in states):
        status = "succeeded"
    elif any(state in {"running"} for state in states):
        status = "running"
    elif any(state == "queued" for state in states):
        status = "queued"
    elif any(state in {"failed", "blocked"} for state in states):
        status = "failed"
    else:
        status = run.status
    run.status = status
    # Experiment summaries retain exact child rollouts; sum only known values.
    from harnessa.db.harness import HarnessProbeRun
    probe_rows = list(await db.scalars(select(HarnessProbeRun).where(
        HarnessProbeRun.experiment_id.in_([row.id for row in experiments])
    ))) if experiments else []
    known_costs = [Decimal(row.cost_usd) for row in probe_rows if row.cost_usd is not None]
    latencies = sorted(row.wall_ms for row in probe_rows if row.wall_ms is not None)
    def percentile(q: float) -> int | None:
        if not latencies:
            return None
        rank = max(1, ceil(len(latencies) * q))
        return latencies[min(len(latencies) - 1, rank - 1)]

    mode = run.manifest["mode"]
    k = int(run.manifest["k"])
    # Each child shares one baseline across both lanes and runs one candidate plus
    # one composed rollout per lane. The baseline must not be counted twice.
    per_experiment_samples = 1 + (2 if mode == "both" else 1) * (k + 1)
    expected_samples = len(experiments) * per_experiment_samples
    observed_samples = len(probe_rows)
    delivered = [row.delivered for row in probe_rows if row.delivered is not None]
    successes = sum(delivered)
    interval: list[float] | None = None
    if delivered:
        n = len(delivered)
        z = 1.96
        proportion = successes / n
        denominator = 1 + z * z / n
        center = (proportion + z * z / (2 * n)) / denominator
        margin = z * ((proportion * (1 - proportion) / n + z * z / (4 * n * n)) ** 0.5) / denominator
        interval = [max(0.0, center - margin), min(1.0, center + margin)]
    if status in {"succeeded", "failed", "cancelled"}:
        run.completed_at = run.completed_at or datetime.now(UTC)
    return EvaluationRunRead(
        id=run.id, stable_key=run.stable_key, status=status, manifest=run.manifest,
        manifest_digest=run.manifest_digest, sample_size=expected_samples,
        completed_samples=observed_samples,
        failed_samples=sum(row.status == "failed" for row in probe_rows),
        cancelled_samples=sum(row.status == "cancelled" for row in probe_rows),
        unknown_samples=sum(row.status == "unknown" for row in probe_rows),
        missing_samples=max(0, expected_samples - observed_samples),
        delivery_samples=len(delivered), delivery_successes=successes,
        delivery_wilson_95=interval,
        cost_usd=str(sum(known_costs)) if known_costs else None,
        p50_latency_ms=percentile(0.5), p95_latency_ms=percentile(0.95),
        experiment_ids=run.experiment_ids, created_at=run.created_at,
    )
