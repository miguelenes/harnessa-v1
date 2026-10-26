"""Authorized, durable Harness² experiment and learning controls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.harness2 import UPSTREAM_REVISION, product_manifest
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import TaskStage
from harnessa.db.harness import (
    EvaluationRun,
    HarnessExperience,
    HarnessExperiment,
    HarnessPolicy,
    HarnessProbeRun,
    HarnessPromotion,
    SkillConflictResolution,
)
from harnessa.db.models import Membership, Project, Repository, Task, User
from harnessa.db.session import get_db
from harnessa.db.specification import SpecificationInterview
from harnessa.domain.skill_evolution import (
    compatibility_snapshot,
    scopes_overlap,
    validate_provenance,
    validate_skill_artifact,
)
from harnessa.services.authorization import require_membership
from harnessa.services.skill_compatibility import route_capability_revision, route_manifest
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
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


class SkillImportRequest(BaseModel):
    source_kind: Literal["imported", "operator_authored"]
    scope_type: Literal["task", "repository", "project", "organization"]
    scope_id: str = Field(min_length=1, max_length=300)
    route_alias: str = Field(min_length=1, max_length=120)
    skill_path: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=64_000)


class ConflictResolutionRequest(BaseModel):
    stable_key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=10, max_length=2000)


class HarnessOverview(BaseModel):
    manifest: dict[str, Any]
    policy: dict[str, Any]
    experiments: list[dict[str, Any]]
    probes: list[dict[str, Any]]
    experiences: list[dict[str, Any]]


async def _skill_evaluation_gate(
    db: AsyncSession, project_id: UUID, candidate: HarnessExperience
) -> dict[str, Any] | None:
    if candidate.license_id == "UNKNOWN" or candidate.conflict_state != "clear":
        return None
    compatibility = candidate.compatibility_snapshot
    route_alias = compatibility.get("route_alias")
    capability_revision = compatibility.get("model_capability_revision")
    if not isinstance(route_alias, str) or not route_alias or not isinstance(capability_revision, str) or not capability_revision:
        return None
    evaluations = list(await db.scalars(select(EvaluationRun).where(
        EvaluationRun.project_id == project_id,
        EvaluationRun.status == "succeeded",
    ).order_by(EvaluationRun.created_at.desc()).limit(100)))
    for evaluation in evaluations:
        manifest = evaluation.manifest
        route = manifest.get("model_route_policy") if isinstance(manifest, dict) else None
        if (
            not isinstance(route, dict)
            or route.get("alias") != route_alias
            or route_capability_revision(route) != capability_revision
        ):
            continue
        versions = manifest.get("skill_versions") if isinstance(manifest, dict) else None
        if not isinstance(versions, list) or not any(
            isinstance(version, dict)
            and version.get("id") == str(candidate.id)
            and version.get("version") == candidate.version
            and version.get("candidate_digest") == candidate.candidate_digest
            and version.get("artifact_digest") == candidate.artifact_digest
            for version in versions
        ):
            continue
        task_inputs = manifest.get("task_inputs")
        if not isinstance(task_inputs, list) or len(task_inputs) < 2:
            continue
        children = list(await db.scalars(select(HarnessExperiment).where(
            HarnessExperiment.evaluation_run_id == evaluation.id,
            HarnessExperiment.project_id == project_id,
        ).order_by(HarnessExperiment.task_id)))
        if (
            len(children) != len(task_inputs)
            or len(evaluation.experiment_ids) != len(task_inputs)
            or any(child.status != "succeeded" for child in children)
        ):
            continue
        child_evidence: list[dict[str, Any]] = []
        valid = True
        for child in children:
            result = child.result if isinstance(child.result, dict) else {}
            holdout = result.get("holdout_evaluation")
            holdout_manifest = result.get("holdout_manifest")
            skill_versions = holdout_manifest.get("skill_versions") if isinstance(holdout_manifest, dict) else None
            if (
                result.get("comparison_successful") is not True
                or result.get("composed_successful") is not True
                or not isinstance(holdout, dict)
                or holdout.get("status") != "passed"
                or not isinstance(holdout_manifest, dict)
                or holdout.get("manifest_digest") != holdout_manifest.get("digest")
                or not isinstance(skill_versions, list)
                or not any(
                    isinstance(version, dict)
                    and version.get("experience_id") == str(candidate.id)
                    and version.get("version") == candidate.version
                    and version.get("digest") == candidate.artifact_digest
                    for version in skill_versions
                )
            ):
                valid = False
                break
            child_evidence.append({
                "experiment_id": str(child.id),
                "result_digest": child.result_digest,
                "holdout_manifest_digest": holdout_manifest["digest"],
                "holdout_receipt_digest": holdout.get("result_digest"),
            })
        if not valid:
            continue
        probes = list(await db.scalars(select(HarnessProbeRun).where(
            HarnessProbeRun.project_id == project_id,
            HarnessProbeRun.experiment_id.in_([child.id for child in children]),
        )))
        if not probes or any(probe.cost_usd is None for probe in probes):
            continue
        return {
            "evaluation_run_id": str(evaluation.id),
            "manifest_digest": evaluation.manifest_digest,
            "sample_size": len(task_inputs),
            "missing_tasks": 0,
            "failed_tasks": 0,
            "unknown_cost_or_outcome": 0,
            "candidate_digest": candidate.candidate_digest,
            "regression": "passed",
            "security": "passed",
            "cost": "passed",
            "evidence": child_evidence,
        }
    return None


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
            .where(or_(
                HarnessExperience.project_id == project_id,
                (HarnessExperience.organization_id == project.organization_id)
                & (HarnessExperience.scope_type == "organization")
                & (HarnessExperience.scope_id == str(project.organization_id)),
            ))
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
                "conflict_state": row.conflict_state,
                "source_kind": row.source_kind,
                "author": row.author,
                "producer": row.producer,
                "source_author_id": str(row.source_author_id) if row.source_author_id else None,
                "source_task_id": str(row.source_task_id) if row.source_task_id else None,
                "source_experiment_id": str(row.source_experiment_id) if row.source_experiment_id else None,
                "source_run_id": str(row.source_run_id) if row.source_run_id else None,
                "source_evaluation_run_id": str(row.source_evaluation_run_id) if row.source_evaluation_run_id else None,
                "scope_type": row.scope_type,
                "scope_id": row.scope_id,
                "license_id": row.license_id,
                "dependencies": row.dependencies,
                "allowed_tools": row.allowed_tools,
                "trust_level": row.trust_level,
                "artifact_path": row.artifact_path,
                "artifact_digest": row.artifact_digest,
                "artifact_size_bytes": row.artifact_size_bytes,
                "confidence_rationale": row.confidence_rationale,
                "compatibility_snapshot": row.compatibility_snapshot,
                "stale_reason": row.stale_reason,
                "stale_at": row.stale_at.isoformat() if row.stale_at else None,
                "expiry_at": row.expiry_at.isoformat(),
                "revalidated_at": row.revalidated_at.isoformat() if row.revalidated_at else None,
            }
            for row in experiences
        ],
    )


@router.post("/projects/{project_id}/skills", status_code=201)
async def create_skill_candidate(
    project_id: UUID,
    body: SkillImportRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Import an untrusted immutable skill version for explicit review."""
    await _operator(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    try:
        artifact = validate_skill_artifact(body.skill_path, body.content)
        validate_provenance(body.source_kind, body.scope_type, "untrusted")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        frozen_route = await route_manifest(db, project.organization_id, body.route_alias)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    capability_revision = route_capability_revision(frozen_route)
    if body.scope_type == "task":
        try:
            task_id = UUID(body.scope_id)
        except ValueError as exc:
            raise HTTPException(422, "Task scope must be a task UUID in this project") from exc
        task = await db.scalar(select(Task).where(Task.id == task_id, Task.project_id == project_id))
        if task is None:
            raise HTTPException(422, "Task scope must identify a task in this project")
        interview = await db.scalar(select(SpecificationInterview).where(
            SpecificationInterview.task_id == task.id
        ))
        repository = await db.get(Repository, interview.repository_id) if interview else None
        repository_scope = repository.target_id if repository is not None else "*"
        scope_id = str(task.id)
    elif body.scope_type == "repository":
        repository = await db.scalar(select(Repository).where(
            Repository.project_id == project_id,
            Repository.target_id == body.scope_id,
        ))
        if repository is None:
            raise HTTPException(422, "Repository scope must identify a repository in this project")
        repository_scope = repository.target_id
        scope_id = repository.target_id
    elif body.scope_type == "project":
        if body.scope_id != str(project_id):
            raise HTTPException(422, "Project scope must match the requested project")
        repository_scope = "*"
        scope_id = str(project_id)
    else:
        membership = await require_membership(db, actor, project_id)
        if not isinstance(membership, Membership) or membership.role != "administrator":
            raise HTTPException(403, "Organization-scoped skills require an organization administrator")
        if body.scope_id != str(project.organization_id):
            raise HTTPException(422, "Organization scope must match the project's organization")
        repository_scope = "*"
        scope_id = str(project.organization_id)
    key = hashlib.sha256(
        f"{body.scope_type}:{scope_id}:{artifact.slug}".encode()
    ).hexdigest()[:32]
    if body.scope_type == "organization" and db.get_bind().dialect.name == "postgresql":
        lock_material = f"{project.organization_id}:{key}".encode()
        lock_id = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
        await db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    version_query = select(HarnessExperience).where(
        HarnessExperience.experience_key == key,
        HarnessExperience.scope_type == body.scope_type,
        HarnessExperience.scope_id == scope_id,
    )
    if body.scope_type == "organization":
        version_query = version_query.where(
            HarnessExperience.organization_id == project.organization_id
        )
    else:
        version_query = version_query.where(HarnessExperience.project_id == project_id)
    versions = list(await db.scalars(
        version_query.order_by(HarnessExperience.version.desc()).with_for_update()
    ))
    duplicate = next((
        row for row in versions
        if row.artifact_digest == artifact.digest and row.status != "rejected"
    ), None)
    if duplicate is not None:
        return {
            "id": str(duplicate.id),
            "status": duplicate.status,
            "version": duplicate.version,
            "candidate_digest": duplicate.candidate_digest,
            "idempotent_replay": True,
        }
    policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == project_id))
    policy_revision = policy.revision if policy is not None else 1
    overlapping_candidates = list(await db.scalars(select(HarnessExperience).where(
        HarnessExperience.organization_id == project.organization_id,
        HarnessExperience.conflict_key == artifact.slug,
        HarnessExperience.status == "candidate",
        or_(
            HarnessExperience.project_id == project_id,
            HarnessExperience.scope_type == "organization",
        ),
    ).with_for_update()))
    conflict = any(
        row.artifact_digest != artifact.digest
        and scopes_overlap(
            body.scope_type,
            scope_id,
            repository_scope,
            row.scope_type,
            row.scope_id,
            row.repository_scope,
        )
        for row in overlapping_candidates
    )
    stored = LocalArtifactStore(Path(settings.artifact_root)).put_bytes(
        artifact.content.encode("utf-8"), media_type="text/markdown; charset=utf-8"
    )
    candidate_identity = {
        "artifact_digest": stored.digest,
        "author_id": str(actor.id),
        "license_id": artifact.license_id,
        "scope_id": scope_id,
        "scope_type": body.scope_type,
    }
    candidate_digest = hashlib.sha256(
        json.dumps(candidate_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = HarnessExperience(
        organization_id=project.organization_id,
        project_id=(
            versions[0].project_id
            if body.scope_type == "organization" and versions
            else project_id
        ),
        source_task_id=None,
        source_experiment_id=None,
        source_run_id=None,
        source_evaluation_run_id=None,
        experience_key=key,
        version=(versions[0].version + 1) if versions else 1,
        status="candidate",
        repository_scope=repository_scope,
        domain_scope="skill",
        cue=artifact.slug,
        lesson=artifact.description,
        evidence_refs=[],
        candidate_digest=candidate_digest,
        conflict_key=artifact.slug,
        conflict_state="conflict" if conflict else "clear",
        source_kind=body.source_kind,
        author=actor.email,
        producer="harnessa-skill-import-v1",
        source_author_id=actor.id,
        scope_type=body.scope_type,
        scope_id=scope_id,
        license_id=artifact.license_id,
        dependencies=list(artifact.dependencies),
        allowed_tools=list(artifact.declared_tools),
        trust_level="untrusted",
        artifact_path=artifact.path,
        artifact_digest=stored.digest,
        artifact_storage_key=stored.storage_key,
        artifact_size_bytes=stored.size_bytes,
        confidence_rationale="Imported or operator-authored skill; no task or holdout evidence is implied.",
        compatibility_snapshot=compatibility_snapshot(
            harness2_revision=UPSTREAM_REVISION,
            tool_revision=settings.acp_harness_revision,
            policy_revision=policy_revision,
            model_capability_revision=capability_revision,
            route_alias=body.route_alias,
            executor_image=settings.acp_agent_image,
            repository=(
                repository_scope if body.scope_type in {"repository", "task"} else None
            ),
            task=scope_id if body.scope_type == "task" else None,
            organization=(scope_id if body.scope_type == "organization" else None),
        ),
        expiry_at=datetime.now(UTC) + timedelta(days=180),
        policy_revision=policy_revision,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "status": row.status,
        "version": row.version,
        "candidate_digest": row.candidate_digest,
        "artifact_digest": row.artifact_digest,
        "conflict_state": row.conflict_state,
        "automatically_activated": False,
        "declared_tools_grant_capabilities": False,
    }


@router.post("/projects/{project_id}/harness-experiences/{experience_id}/resolve-conflict")
async def resolve_skill_conflict(
    project_id: UUID,
    experience_id: UUID,
    body: ConflictResolutionRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _operator(db, actor, project_id, admin=True)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    prior = await db.scalar(select(SkillConflictResolution).where(
        SkillConflictResolution.project_id == project_id,
        SkillConflictResolution.stable_key == body.stable_key,
    ))
    if prior is not None:
        if prior.target_experience_id != experience_id:
            raise HTTPException(409, "Conflict resolution key is already bound to another group")
        return {
            "decision": prior.action,
            "candidate_ids": prior.candidate_ids,
            "reimport_required": True,
        }
    target = await db.scalar(select(HarnessExperience).where(
        HarnessExperience.id == experience_id,
        HarnessExperience.organization_id == project.organization_id,
        or_(
            HarnessExperience.project_id == project_id,
            HarnessExperience.scope_type == "organization",
        ),
    ).with_for_update())
    if target is None:
        raise HTTPException(404, "Conflicting skill candidate not found")
    if target.status != "candidate" or target.conflict_state != "conflict":
        raise HTTPException(409, "Only an unresolved conflicting candidate can be resolved")
    rows = list(await db.scalars(select(HarnessExperience).where(
        HarnessExperience.organization_id == project.organization_id,
        HarnessExperience.conflict_key == target.conflict_key,
        HarnessExperience.status == "candidate",
        or_(
            HarnessExperience.project_id == project_id,
            HarnessExperience.scope_type == "organization",
        ),
    ).with_for_update()))
    group = [row for row in rows if scopes_overlap(
        target.scope_type,
        target.scope_id,
        target.repository_scope,
        row.scope_type,
        row.scope_id,
        row.repository_scope,
    )]
    if len(group) < 2:
        raise HTTPException(409, "The conflicting candidate group is no longer unresolved")
    candidate_ids = sorted(str(row.id) for row in group)
    for row in group:
        row.status = "rejected"
    db.add(SkillConflictResolution(
        organization_id=project.organization_id,
        project_id=project_id,
        actor_id=actor.id,
        target_experience_id=target.id,
        conflict_key=target.conflict_key,
        stable_key=body.stable_key,
        action="reject_group",
        rationale=body.rationale.strip(),
        candidate_ids=candidate_ids,
        created_at=datetime.now(UTC),
    ))
    await db.commit()
    return {
        "decision": "reject_group",
        "candidate_ids": candidate_ids,
        "reimport_required": True,
    }


@router.get("/projects/{project_id}/harness-experiences/{experience_id}/artifact")
async def read_skill_artifact(
    project_id: UUID,
    experience_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    await require_membership(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    experience = await db.scalar(select(HarnessExperience).where(
        HarnessExperience.id == experience_id,
        or_(
            HarnessExperience.project_id == project_id,
            (HarnessExperience.organization_id == project.organization_id)
            & (HarnessExperience.scope_type == "organization")
            & (HarnessExperience.scope_id == str(project.organization_id)),
        ),
    ))
    if experience is None:
        raise HTTPException(404, "Skill artifact not found")
    if not experience.artifact_storage_key or not experience.artifact_digest or not experience.artifact_size_bytes:
        raise HTTPException(404, "This experience has no skill artifact")
    store = LocalArtifactStore(Path(settings.artifact_root))
    state = store.verify_reference(
        experience.artifact_storage_key,
        expected_digest=experience.artifact_digest,
        expected_size=experience.artifact_size_bytes,
        max_bytes=64_000,
    )
    if state != "verified":
        raise HTTPException(409, "Skill artifact failed content-addressed verification")
    content = (store.root / experience.artifact_storage_key).read_text(encoding="utf-8")
    try:
        artifact = validate_skill_artifact(experience.artifact_path or "", content)
    except ValueError as exc:
        raise HTTPException(409, "Skill artifact schema is invalid") from exc
    if artifact.digest != experience.artifact_digest:
        raise HTTPException(409, "Skill artifact content changed")
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


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
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
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
            or_(
                HarnessExperience.project_id == project_id,
                (HarnessExperience.organization_id == project.organization_id)
                & (HarnessExperience.scope_type == "organization")
                & (HarnessExperience.scope_id == str(project.organization_id)),
            ),
        )
        .with_for_update()
    )
    if candidate is None:
        raise HTTPException(404, "Experience candidate not found")
    if candidate.status != "candidate" or candidate.expiry_at <= datetime.now(UTC):
        raise HTTPException(409, "Only an unexpired candidate experience can be promoted")
    if candidate.scope_type == "task":
        raise HTTPException(409, "Task-scoped skills cannot be activated across task boundaries")
    if candidate.scope_type == "organization":
        membership = await require_membership(db, actor, project_id)
        if not isinstance(membership, Membership) or membership.role != "administrator":
            raise HTTPException(403, "Organization-scoped promotion requires an organization administrator")
    if candidate.candidate_digest != body.candidate_digest:
        raise HTTPException(409, "Candidate digest does not match the reviewed version")
    competing_candidates = list(await db.scalars(select(HarnessExperience).where(
        HarnessExperience.organization_id == candidate.organization_id,
        HarnessExperience.conflict_key == candidate.conflict_key,
        HarnessExperience.status == "candidate",
        HarnessExperience.id != candidate.id,
        or_(
            HarnessExperience.project_id == project_id,
            HarnessExperience.scope_type == "organization",
        ),
    )))
    if any(
        item.artifact_digest != candidate.artifact_digest
        and scopes_overlap(
            candidate.scope_type,
            candidate.scope_id,
            candidate.repository_scope,
            item.scope_type,
            item.scope_id,
            item.repository_scope,
        )
        for item in competing_candidates
    ):
        raise HTTPException(409, "Conflicting skill candidates require an explicit review decision")
    experiment: HarnessExperiment | None = None
    if candidate.artifact_digest is not None:
        promotion_gates = await _skill_evaluation_gate(db, project_id, candidate)
        if promotion_gates is None:
            raise HTTPException(
                409,
                "Skill promotion requires a matching, passing multi-task evaluation with known costs",
            )
    else:
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
        promotion_gates = {
            "candidate_digest": candidate.candidate_digest,
            **experiment.result["promotion_gates"],
        }
    policy = await db.scalar(
        select(HarnessPolicy).where(HarnessPolicy.project_id == project_id).with_for_update()
    )
    if policy is None or policy.reviewed_promotion is not True:
        raise HTTPException(409, "Reviewed promotion policy is unavailable")
    if policy.revision != candidate.policy_revision:
        raise HTTPException(409, "Candidate policy revision is stale and requires revalidation")
    active_query = select(HarnessExperience).where(
        HarnessExperience.experience_key == candidate.experience_key,
        HarnessExperience.status == "active",
    )
    if candidate.scope_type == "organization":
        active_query = active_query.where(
            HarnessExperience.organization_id == candidate.organization_id,
            HarnessExperience.scope_type == "organization",
            HarnessExperience.scope_id == candidate.scope_id,
        )
    else:
        active_query = active_query.where(
            HarnessExperience.project_id == project_id,
            HarnessExperience.repository_scope == candidate.repository_scope,
        )
    active = await db.scalar(
        active_query.with_for_update()
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
            "promotion_gates": promotion_gates,
            "skill_version": {
                "artifact_digest": candidate.artifact_digest,
                "version": candidate.version,
                "scope_type": candidate.scope_type,
                "scope_id": candidate.scope_id,
            },
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
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
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
            or_(
                HarnessExperience.project_id == project_id,
                (HarnessExperience.organization_id == project.organization_id)
                & (HarnessExperience.scope_type == "organization")
                & (HarnessExperience.scope_id == str(project.organization_id)),
            ),
        )
        .with_for_update()
    )
    if target is None or target.status != "superseded":
        raise HTTPException(409, "Rollback target must be a prior superseded version")
    if target.candidate_digest != body.candidate_digest:
        raise HTTPException(409, "Rollback digest does not match the prior version")
    current_query = select(HarnessExperience).where(
        HarnessExperience.experience_key == target.experience_key,
        HarnessExperience.status == "active",
    )
    if target.scope_type == "organization":
        current_query = current_query.where(
            HarnessExperience.organization_id == target.organization_id,
            HarnessExperience.scope_type == "organization",
            HarnessExperience.scope_id == target.scope_id,
        )
    else:
        current_query = current_query.where(
            HarnessExperience.project_id == project_id,
            HarnessExperience.repository_scope == target.repository_scope,
        )
    current = await db.scalar(
        current_query.with_for_update()
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
