"""Authorized, durable Harness² experiment and learning controls."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from harnessa.adapters.harness2 import UPSTREAM_REVISION, product_manifest
from harnessa.auth.users import current_active_user
from harnessa.contracts import TaskStage
from harnessa.db.harness import (
    HarnessExperience,
    HarnessExperiment,
    HarnessPolicy,
    HarnessProbeRun,
    HarnessPromotion,
)
from harnessa.db.models import Project, Task, User
from harnessa.db.session import get_db
from harnessa.db.specification import SpecificationInterview
from harnessa.services.authorization import require_membership
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["harness learning"])


class ExperimentCreate(BaseModel):
    stable_key: str = Field(min_length=1, max_length=200)
    mode: str = Field(pattern=r"^(parallel|sequential|both)$")
    k: int = Field(ge=1, le=3)
    route_alias: str = Field(min_length=1, max_length=120)


class ExperimentRead(BaseModel):
    id: UUID
    task_id: UUID
    status: str
    phase: str
    mode: str
    k: int
    base_sha: str
    specification_hash: str
    specification_revision: int
    policy_revision: int
    upstream_revision: str
    result: dict[str, Any]
    created_at: datetime


class PromotionRequest(BaseModel):
    stable_key: str = Field(min_length=1, max_length=200)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=10, max_length=2000)


class HarnessOverview(BaseModel):
    manifest: dict[str, Any]
    policy: dict[str, Any]
    experiments: list[dict[str, Any]]
    probes: list[dict[str, Any]]
    experiences: list[dict[str, Any]]


def _promotion_gates_passed(result: dict[str, Any], candidate_digest: str) -> bool:
    evidence = result.get("promotion_gates")
    holdout = result.get("holdout_evaluation")
    manifest = result.get("holdout_manifest")
    return (
        isinstance(evidence, dict)
        and evidence.get("candidate_digest") == candidate_digest
        and evidence.get("regression") == "passed"
        and evidence.get("security") == "passed"
        and evidence.get("cost") == "passed"
        and isinstance(manifest, dict)
        and isinstance(holdout, dict)
        and holdout.get("status") == "passed"
        and holdout.get("manifest_digest") == manifest.get("digest")
        and isinstance(holdout.get("judge_revision"), str)
        and bool(holdout["judge_revision"])
    )


def apply_reviewed_experience_transition(target: Any, current: Any | None, action: str) -> None:
    """Switch active experience versions only for a valid reviewed promote/rollback."""
    valid_target = target.status == ("candidate" if action == "promote" else "superseded")
    if action not in {"promote", "rollback"} or not valid_target:
        raise ValueError("Reviewed experience transition has an invalid target state")
    if action == "rollback" and (current is None or current.status != "active"):
        raise ValueError("Rollback requires a current active version")
    if current is not None:
        current.status = "superseded"
    target.status = "active"


def _experiment_read(row: HarnessExperiment) -> ExperimentRead:
    return ExperimentRead(
        id=row.id,
        task_id=row.task_id,
        status=row.status,
        phase=row.phase,
        mode=row.mode,
        k=row.k,
        base_sha=row.base_sha,
        specification_hash=row.specification_hash,
        specification_revision=row.specification_revision,
        policy_revision=row.policy_revision,
        upstream_revision=row.harness2_revision,
        result=row.result,
        created_at=row.created_at,
    )


async def _operator(
    db: AsyncSession, actor: User, project_id: UUID, *, admin: bool = False
) -> None:
    membership = await require_membership(db, actor, project_id)
    role = getattr(membership, "role", "")
    if role not in ({"administrator"} if admin else {"administrator", "operator"}):
        raise HTTPException(403, "Harness operation requires an authorized project operator")


@router.get("/projects/{project_id}/harness")
async def harness_overview(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> HarnessOverview:
    await require_membership(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    experiments = list(
        await db.scalars(
            select(HarnessExperiment)
            .where(HarnessExperiment.project_id == project_id)
            .order_by(HarnessExperiment.created_at.desc())
            .limit(100)
        )
    )
    probes = list(
        await db.scalars(
            select(HarnessProbeRun)
            .where(HarnessProbeRun.project_id == project_id)
            .order_by(HarnessProbeRun.created_at.desc())
            .limit(500)
        )
    )
    experiences = list(
        await db.scalars(
            select(HarnessExperience)
            .where(HarnessExperience.project_id == project_id)
            .order_by(HarnessExperience.created_at.desc())
            .limit(500)
        )
    )
    return HarnessOverview(
        manifest=product_manifest(
            policy_revision="fixed-v1",
            policy_digest=hashlib.sha256(b"harnessa-host-policy-v1").hexdigest(),
        ).__dict__,
        policy={
            "max_k": policy.max_k,
            "max_probe_concurrency": policy.max_probe_concurrency,
            "reviewed_promotion": True,
            "revision": policy.revision,
        }
        if policy
        else {"max_k": 3, "max_probe_concurrency": 3, "reviewed_promotion": True, "revision": 1},
        experiments=[_experiment_read(row).model_dump(mode="json") for row in experiments],
        probes=[
            {
                "id": str(row.id),
                "experiment_id": str(row.experiment_id),
                "label": row.label,
                "phase": row.phase,
                "status": row.status,
                "delivered": row.delivered,
                "wall_ms": row.wall_ms,
                "tokens": row.tokens,
                "cost_usd": str(row.cost_usd) if row.cost_usd is not None else None,
                "created_at": row.created_at.isoformat(),
                "route_alias": row.route_alias,
                "harness_digest": row.harness_digest,
                "trace_digest": row.trace_digest,
                "component_diff": row.component_diff,
                "outcome_detail": row.outcome_detail,
            }
            for row in probes
        ],
        experiences=[
            {
                "id": str(row.id),
                "experience_key": row.experience_key,
                "version": row.version,
                "status": row.status,
                "repository_scope": row.repository_scope,
                "domain_scope": row.domain_scope,
                "cue": row.cue,
                "lesson": row.lesson,
                "evidence_refs": row.evidence_refs,
                "candidate_digest": row.candidate_digest,
                "conflict_key": row.conflict_key,
                "expiry_at": row.expiry_at.isoformat(),
                "revalidated_at": row.revalidated_at.isoformat() if row.revalidated_at else None,
            }
            for row in experiences
        ],
    )


@router.post("/projects/{project_id}/tasks/{task_id}/harness-experiments", status_code=202)
async def create_harness_experiment(
    project_id: UUID,
    task_id: UUID,
    body: ExperimentCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExperimentRead:
    await _operator(db, actor, project_id)
    task = await db.scalar(
        select(Task).where(Task.id == task_id, Task.project_id == project_id).with_for_update()
    )
    if task is None:
        raise HTTPException(404, "Task not found")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if task.stage != TaskStage.HARNESS_PROBE.value:
        raise HTTPException(409, "Task must be in HARNESS_PROBE before an experiment can be queued")
    interview = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == task_id)
    )
    if interview is None or interview.readiness_state != "READY":
        raise HTTPException(409, "A ready frozen specification is required")
    policy = await db.scalar(
        select(HarnessPolicy).where(HarnessPolicy.project_id == project_id).with_for_update()
    )
    if policy is None:
        policy = HarnessPolicy(organization_id=project.organization_id, project_id=project_id)
        db.add(policy)
        await db.flush()
    if body.k > policy.max_k or policy.reviewed_promotion is not True:
        raise HTTPException(422, "Experiment exceeds fixed project harness policy")
    prior = await db.scalar(
        select(HarnessExperiment).where(
            HarnessExperiment.project_id == project_id,
            HarnessExperiment.stable_key == body.stable_key,
        )
    )
    spec_hash = interview.artifact_hash
    if prior:
        if (prior.task_id, prior.base_sha, prior.specification_hash, prior.mode, prior.k) != (
            task_id,
            interview.base_sha,
            spec_hash,
            body.mode,
            body.k,
        ):
            raise HTTPException(
                409, "Stable experiment key is already bound to different frozen inputs"
            )
        return _experiment_read(prior)
    row = HarnessExperiment(
        organization_id=project.organization_id,
        project_id=project_id,
        task_id=task_id,
        created_by=actor.id,
        stable_key=body.stable_key,
        mode=body.mode,
        k=body.k,
        status="queued",
        phase="queued",
        attempts=0,
        base_sha=interview.base_sha,
        specification_hash=spec_hash,
        specification_revision=interview.revision,
        policy_revision=policy.revision,
        harness2_revision=UPSTREAM_REVISION,
        result={"route_alias": body.route_alias},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _experiment_read(row)


@router.post("/projects/{project_id}/harness-experiments/{experiment_id}/cancel")
async def cancel_harness_experiment(
    project_id: UUID,
    experiment_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExperimentRead:
    await _operator(db, actor, project_id)
    row = await db.scalar(
        select(HarnessExperiment)
        .where(HarnessExperiment.project_id == project_id, HarnessExperiment.id == experiment_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Experiment not found")
    if row.status in {"queued", "running"}:
        row.cancel_requested_at = datetime.now(UTC)
        if row.status == "queued":
            row.status, row.phase, row.completed_at = "cancelled", "cancelled", datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return _experiment_read(row)


@router.post("/projects/{project_id}/harness-experiments/{experiment_id}/resume", status_code=202)
async def resume_harness_experiment(
    project_id: UUID,
    experiment_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExperimentRead:
    """Requeue the same frozen experiment after a recoverable terminal failure."""
    await _operator(db, actor, project_id)
    row = await db.scalar(
        select(HarnessExperiment)
        .where(HarnessExperiment.project_id == project_id, HarnessExperiment.id == experiment_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Experiment not found")
    if row.status not in {"failed", "blocked"}:
        raise HTTPException(409, "Only failed or blocked experiments can be resumed")
    if row.cancel_requested_at is not None:
        raise HTTPException(409, "A cancelled experiment cannot be resumed; queue a new manifest")
    state = dict(row.result or {})
    resume_count = state.get("resume_count", 0)
    if (
        not isinstance(resume_count, int)
        or isinstance(resume_count, bool)
        or resume_count < 0
        or resume_count >= 2
    ):
        raise HTTPException(409, "The experiment resume limit has been reached")
    task = await db.scalar(
        select(Task).where(Task.id == row.task_id, Task.project_id == project_id).with_for_update()
    )
    policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    interview = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == row.task_id)
    )
    if (
        task is None
        or task.stage != TaskStage.HARNESS_PROBE.value
        or interview is None
        or interview.readiness_state != "READY"
        or interview.base_sha != row.base_sha
        or interview.artifact_hash != row.specification_hash
        or interview.revision != row.specification_revision
        or policy is None
        or policy.revision != row.policy_revision
    ):
        raise HTTPException(409, "Frozen task, specification, or evaluation policy changed; queue a new manifest")
    state["resume_count"] = resume_count + 1
    row.result = state
    row.status, row.phase = "queued", "queued"
    row.last_error = None
    row.worker_heartbeat_at = None
    await db.commit()
    await db.refresh(row)
    return _experiment_read(row)


@router.post("/projects/{project_id}/harness-experiences/{experience_id}/promote")
async def promote_harness_experience(
    project_id: UUID,
    experience_id: UUID,
    body: PromotionRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _operator(db, actor, project_id, admin=True)
    previous = await db.scalar(
        select(HarnessPromotion).where(
            HarnessPromotion.project_id == project_id,
            HarnessPromotion.stable_key == body.stable_key,
        )
    )
    if previous:
        if previous.experience_id != experience_id or previous.action != "promote":
            raise HTTPException(409, "Promotion key is already bound to another decision")
        return {"decision": previous.decision, "experience_id": str(previous.experience_id)}
    candidate = await db.scalar(
        select(HarnessExperience)
        .where(
            HarnessExperience.id == experience_id,
            HarnessExperience.project_id == project_id,
        )
        .with_for_update()
    )
    if candidate is None:
        raise HTTPException(404, "Experience candidate not found")
    if candidate.status != "candidate" or candidate.expiry_at <= datetime.now(UTC):
        raise HTTPException(409, "Only an unexpired candidate experience can be promoted")
    if candidate.candidate_digest != body.candidate_digest:
        raise HTTPException(409, "Candidate digest does not match the reviewed version")
    experiment = await db.scalar(
        select(HarnessExperiment).where(
            HarnessExperiment.id == candidate.source_experiment_id,
            HarnessExperiment.project_id == project_id,
            HarnessExperiment.status == "succeeded",
        )
    )
    if experiment is None or not _promotion_gates_passed(
        experiment.result, candidate.candidate_digest
    ):
        raise HTTPException(
            409, "Exact candidate has no passing fixed regression, security, and cost evidence"
        )
    policy = await db.scalar(
        select(HarnessPolicy).where(HarnessPolicy.project_id == project_id).with_for_update()
    )
    if policy is None or policy.reviewed_promotion is not True:
        raise HTTPException(409, "Reviewed promotion policy is unavailable")
    if policy.revision != candidate.policy_revision:
        raise HTTPException(409, "Candidate policy revision is stale and requires revalidation")
    active = await db.scalar(
        select(HarnessExperience)
        .where(
            HarnessExperience.project_id == project_id,
            HarnessExperience.experience_key == candidate.experience_key,
            HarnessExperience.repository_scope == candidate.repository_scope,
            HarnessExperience.status == "active",
        )
        .with_for_update()
    )
    apply_reviewed_experience_transition(candidate, active, "promote")
    candidate.revalidated_at = datetime.now(UTC)
    decision = HarnessPromotion(
        organization_id=candidate.organization_id,
        project_id=project_id,
        experience_id=candidate.id,
        previous_experience_id=active.id if active else None,
        actor_id=actor.id,
        stable_key=body.stable_key,
        action="promote",
        decision="approved",
        rationale=body.rationale,
        policy_revision=policy.revision,
        gate_evidence={
            "reviewed": True,
            "candidate_digest": candidate.candidate_digest,
            "promotion_gates": experiment.result["promotion_gates"],
            "regression_gate_revision": policy.regression_gate_revision,
            "security_gate_revision": policy.security_gate_revision,
        },
    )
    db.add(decision)
    await db.commit()
    return {
        "decision": "approved",
        "experience_id": str(candidate.id),
        "previous_experience_id": str(active.id) if active else None,
    }


@router.post("/projects/{project_id}/harness-experiences/{experience_id}/rollback")
async def rollback_harness_experience(
    project_id: UUID,
    experience_id: UUID,
    body: PromotionRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _operator(db, actor, project_id, admin=True)
    key = await db.scalar(
        select(HarnessPromotion).where(
            HarnessPromotion.project_id == project_id,
            HarnessPromotion.stable_key == body.stable_key,
        )
    )
    if key:
        if key.experience_id != experience_id or key.action != "rollback":
            raise HTTPException(409, "Rollback key is already bound to another decision")
        return {"decision": key.decision, "experience_id": str(key.experience_id)}
    target = await db.scalar(
        select(HarnessExperience)
        .where(
            HarnessExperience.id == experience_id,
            HarnessExperience.project_id == project_id,
        )
        .with_for_update()
    )
    if target is None or target.status != "superseded":
        raise HTTPException(409, "Rollback target must be a prior superseded version")
    if target.candidate_digest != body.candidate_digest:
        raise HTTPException(409, "Rollback digest does not match the prior version")
    current = await db.scalar(
        select(HarnessExperience)
        .where(
            HarnessExperience.project_id == project_id,
            HarnessExperience.experience_key == target.experience_key,
            HarnessExperience.repository_scope == target.repository_scope,
            HarnessExperience.status == "active",
        )
        .with_for_update()
    )
    policy = await db.scalar(
        select(HarnessPolicy).where(HarnessPolicy.project_id == project_id).with_for_update()
    )
    if current is None or policy is None or policy.reviewed_promotion is not True:
        raise HTTPException(409, "Active version or reviewed rollback policy is unavailable")
    apply_reviewed_experience_transition(target, current, "rollback")
    target.revalidated_at = datetime.now(UTC)
    db.add(
        HarnessPromotion(
            organization_id=target.organization_id,
            project_id=project_id,
            experience_id=target.id,
            previous_experience_id=current.id,
            actor_id=actor.id,
            stable_key=body.stable_key,
            action="rollback",
            decision="approved",
            rationale=body.rationale,
            policy_revision=policy.revision,
            gate_evidence={
                "reviewed": True,
                "candidate_digest": target.candidate_digest,
                "rolled_back_from": str(current.id),
            },
        )
    )
    await db.commit()
    return {
        "decision": "approved",
        "experience_id": str(target.id),
        "rolled_back_from": str(current.id),
    }
