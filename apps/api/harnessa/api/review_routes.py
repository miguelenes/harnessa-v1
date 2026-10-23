"""Project-authorized verification policy approval and review lifecycle routes."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.adapters.verification import VerificationManifest
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    IntegrationEvidenceRead,
    ManualIntegrationRequest,
    PostIntegrationStepRead,
    PostIntegrationStepRequest,
    ReviewDecisionRequest,
    VerificationManifestApprovalRequest,
    VerificationManifestRevisionRead,
)
from harnessa.db.github import GitHubRepositoryLink
from harnessa.db.model_gateway import ModelBudgetPolicy
from harnessa.db.models import Project, Repository, Task, User
from harnessa.db.orchestration import Run, TaskTransition
from harnessa.db.review import (
    IntegrationEvidence,
    PostIntegrationStep,
    ReviewBundle,
    ReviewDecision,
    VerificationExecution,
    VerificationManifestRevision,
)
from harnessa.db.session import get_db
from harnessa.db.specification import SpecificationInterview
from harnessa.services.authorization import require_membership
from harnessa.services.model_gateway import budget_policy_digest
from harnessa.services.post_integration import complete_task_if_ready
from harnessa.services.review_publication import LocalReviewPublicationPort
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
_manifest_workspace = SpecificationWorkspaceManager()


async def _bundle_stale_reason(db: AsyncSession, bundle: ReviewBundle) -> str | None:
    sync_preview = bundle.bundle.get("openspec_sync_preview")
    if (
        not isinstance(sync_preview, dict)
        or sync_preview.get("archive_preview_ready") is not True
        or sync_preview.get("final_head_verified") is not True
        or sync_preview.get("proposed_final_review_head") != bundle.head_sha
        or sync_preview.get("implementation_head_sha")
        != bundle.bundle.get("implementation_head_sha")
    ):
        return "OpenSpec archive is not incorporated into a verified final review head"
    archive_diff = sync_preview.get("archive_diff")
    if not isinstance(archive_diff, str) or hashlib.sha256(
        archive_diff.encode()
    ).hexdigest() != sync_preview.get("diff_sha256"):
        return "OpenSpec archive diff does not match its recorded digest"
    run = await db.get(Run, bundle.run_id)
    repository = await db.get(Repository, bundle.repository_id)
    execution = await db.get(VerificationExecution, bundle.verification_execution_id)
    interview = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == bundle.task_id)
    )
    project = await db.get(Project, bundle.project_id)
    if not run or not repository or not execution or not interview or not project:
        return "Bound run, repository, interview, or verification evidence is missing"
    if (
        run.source_revision != bundle.bundle.get("implementation_head_sha")
        or execution.source_revision != bundle.head_sha
        or not await _manifest_workspace.contains_revision(repository, bundle.head_sha)
    ):
        return "Verified implementation or OpenSpec review revision changed"
    if (
        interview.readiness_state != "READY"
        or interview.artifact_hash != bundle.specification_hash
        or interview.artifact_revision != bundle.specification_revision
        or interview.head_sha != run.specification_commit_sha
    ):
        return "Approved specification changed"
    if (
        run.harness_revision != settings.acp_harness_revision
        or run.agent_image != settings.acp_agent_image
    ):
        return "Selected harness or agent image changed"
    try:
        harness_content = await _manifest_workspace.read_file_at_revision(
            repository, run.base_sha or "", "AGENTS.md", limit_bytes=128_000
        )
    except SpecificationWorkspaceError:
        harness_content = b""
    if hashlib.sha256(harness_content).hexdigest() != run.harness_digest:
        return "Harness instructions changed"
    if await _manifest_workspace.current_base(repository) != bundle.base_sha:
        return "Repository base changed"
    policy = await db.scalar(
        select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project.id)
    )
    if policy is None or run.budget_policy_digest != budget_policy_digest(policy):
        return "Model budget policy changed"
    latest_manifest = await db.scalar(
        select(VerificationManifestRevision)
        .where(VerificationManifestRevision.repository_id == repository.id)
        .order_by(VerificationManifestRevision.revision.desc())
        .limit(1)
    )
    if latest_manifest is None or latest_manifest.id != execution.manifest_revision_id:
        return "Approved verification manifest changed"
    return None


async def _administrator(db: AsyncSession, actor: User, project_id: UUID) -> None:
    membership = await require_membership(db, actor, project_id)
    if membership.role != "administrator":
        raise HTTPException(403, "Only project administrators can approve verification commands")


@router.put(
    "/repositories/{repository_id}/verification-manifest",
    response_model=VerificationManifestRevisionRead,
)
async def approve_verification_manifest(
    repository_id: UUID,
    body: VerificationManifestApprovalRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> VerificationManifestRevisionRead:
    repository = await db.scalar(
        select(Repository).where(Repository.id == repository_id).with_for_update()
    )
    if repository is None:
        raise HTTPException(404, "Repository not found")
    project = await db.get(Project, repository.project_id)
    if project is None:
        raise HTTPException(404, "Repository project not found")
    await _administrator(db, actor, repository.project_id)
    try:
        for fixture_path in body.protected_fixtures:
            from pathlib import PurePosixPath

            parsed = PurePosixPath(fixture_path)
            if (
                parsed.is_absolute()
                or ".." in parsed.parts
                or ".git" in parsed.parts
                or not fixture_path
            ):
                raise ValueError("Protected fixture paths must be safe relative paths")
        fixture_bytes = {
            path: base64.b64decode(encoded, validate=True)
            for path, encoded in body.protected_fixtures.items()
        }
        if sum(map(len, fixture_bytes.values())) > 2_000_000:
            raise ValueError("Protected fixtures exceed the 2 MB policy limit")
        fixture_digest = hashlib.sha256(
            json.dumps(body.protected_fixtures, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        current_base = await _manifest_workspace.current_base(repository)
        if body.source_revision != current_base:
            raise HTTPException(
                409, "Approve a manifest from the repository's current default branch revision"
            )
        source = await _manifest_workspace.read_file_at_revision(
            repository, body.source_revision, body.source_path
        )
        document = json.loads(source)
        if not isinstance(document, dict):
            raise TypeError("Manifest root must be a JSON object")
        latest = await db.scalar(
            select(VerificationManifestRevision)
            .where(VerificationManifestRevision.repository_id == repository_id)
            .order_by(VerificationManifestRevision.revision.desc())
            .limit(1)
        )
        existing = await db.scalar(
            select(VerificationManifestRevision).where(
                VerificationManifestRevision.repository_id == repository_id,
                VerificationManifestRevision.source_revision == body.source_revision,
                VerificationManifestRevision.source_path == body.source_path,
            )
        )
        if existing is not None:
            source_digest = hashlib.sha256(source).hexdigest()
            if existing.source_digest != source_digest:
                raise HTTPException(409, "Approved source revision content changed unexpectedly")
            if existing.protected_fixture_digest != fixture_digest:
                raise HTTPException(
                    409, "Protected fixtures are immutable for an approved revision"
                )
            await db.commit()
            return VerificationManifestRevisionRead.model_validate(existing)
        revision = latest.revision + 1 if latest else 1
        manifest = VerificationManifest.model_validate({**document, "revision": revision})
    except HTTPException:
        await db.rollback()
        raise
    except (
        SpecificationWorkspaceError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        await db.rollback()
        raise HTTPException(422, "Repository verification manifest is missing or invalid") from exc

    row = VerificationManifestRevision(
        organization_id=project.organization_id,
        project_id=repository.project_id,
        repository_id=repository.id,
        revision=revision,
        source_path=body.source_path,
        source_revision=body.source_revision,
        source_digest=hashlib.sha256(source).hexdigest(),
        manifest=manifest.model_dump(mode="json"),
        manifest_digest=manifest.digest,
        protected_fixtures=body.protected_fixtures,
        protected_fixture_digest=fixture_digest,
        approved_by=actor.id,
        approved_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return VerificationManifestRevisionRead.model_validate(row)


@router.get(
    "/repositories/{repository_id}/verification-manifest",
    response_model=VerificationManifestRevisionRead,
)
async def latest_verification_manifest(
    repository_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> VerificationManifestRevisionRead:
    repository = await db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(404, "Repository not found")
    await require_membership(db, actor, repository.project_id)
    row = await db.scalar(
        select(VerificationManifestRevision)
        .where(VerificationManifestRevision.repository_id == repository_id)
        .order_by(VerificationManifestRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(404, "No approved verification manifest exists")
    return VerificationManifestRevisionRead.model_validate(row)


@router.get("/tasks/{task_id}/review-bundle")
async def get_review_bundle(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    bundle = await db.scalar(
        select(ReviewBundle)
        .where(ReviewBundle.task_id == task_id)
        .order_by(ReviewBundle.created_at.desc())
        .limit(1)
    )
    if bundle is None:
        raise HTTPException(404, "No implementation review bundle exists")
    stale_reason = await _bundle_stale_reason(db, bundle)
    if stale_reason:
        bundle.status = "invalidated"
        bundle.invalidated_reason = stale_reason
        await db.commit()
    publication = LocalReviewPublicationPort().publish(bundle)
    return {
        "id": bundle.id,
        "task_id": bundle.task_id,
        "run_id": bundle.run_id,
        "base_sha": bundle.base_sha,
        "head_sha": bundle.head_sha,
        "bundle_digest": bundle.bundle_digest,
        "artifact_digest": bundle.artifact_digest,
        "status": bundle.status,
        "invalidated_reason": bundle.invalidated_reason,
        "bundle": bundle.bundle,
        "publication": {
            "provider": publication.provider,
            "reference": publication.reference,
            "bundle_digest": publication.bundle_digest,
        },
    }


@router.post("/tasks/{task_id}/review-decision")
async def decide_review_bundle(
    task_id: UUID,
    body: ReviewDecisionRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    if membership.role not in {"administrator", "reviewer"}:
        raise HTTPException(403, "Project reviewer role required")
    existing = await db.scalar(
        select(ReviewDecision).where(
            ReviewDecision.actor_id == actor.id,
            ReviewDecision.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.bundle_digest != body.bundle_digest
            or existing.decision != body.decision
            or existing.rationale != body.rationale
        ):
            raise HTTPException(409, "Review idempotency key was used for another decision")
        return {
            "id": existing.id,
            "decision": existing.decision,
            "bundle_digest": existing.bundle_digest,
        }
    bundle = await db.scalar(
        select(ReviewBundle)
        .where(ReviewBundle.task_id == task_id, ReviewBundle.bundle_digest == body.bundle_digest)
        .with_for_update()
    )
    if bundle is None:
        raise HTTPException(404, "Review bundle not found")
    if task.stage != "HUMAN_REVIEW" or bundle.status != "prepared":
        raise HTTPException(409, "The task or review bundle is no longer current")
    execution = await db.get(VerificationExecution, bundle.verification_execution_id)
    stale_reason = await _bundle_stale_reason(db, bundle)
    if stale_reason:
        bundle.status = "invalidated"
        bundle.invalidated_reason = stale_reason
        await db.commit()
        raise HTTPException(409, "Review evidence is stale and must be regenerated")
    if execution is None:
        raise HTTPException(409, "Verification evidence is missing")
    if body.decision == "approve" and (
        execution.outcome != "passed"
        or any(item.get("outcome") != "passed" for item in execution.result.get("criteria", []))
    ):
        raise HTTPException(409, "Approval requires every verification criterion to pass")
    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(404, "Task project not found")
    row = ReviewDecision(
        organization_id=project.organization_id,
        project_id=task.project_id,
        task_id=task.id,
        bundle_id=bundle.id,
        actor_id=actor.id,
        decision=body.decision,
        rationale=body.rationale,
        idempotency_key=body.idempotency_key,
        bundle_digest=bundle.bundle_digest,
        head_sha=bundle.head_sha,
    )
    db.add(row)
    if body.decision == "approve":
        bundle.status = "published"
    elif body.decision == "request_changes":
        before = task.version
        task.stage = "READY"
        task.run_status = "QUEUED"
        task.blocked_reason = "review_changes_requested"
        task.version += 1
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=task.project_id,
                actor_id=actor.id,
                from_stage="HUMAN_REVIEW",
                to_stage="READY",
                from_run_status="SUCCEEDED",
                to_run_status="QUEUED",
                reason=body.rationale,
                command="review_request_changes",
                version_before=before,
                version_after=task.version,
            )
        )
        bundle.status = "invalidated"
        bundle.invalidated_reason = "Reviewer requested changes"
    await db.commit()
    return {"id": row.id, "decision": row.decision, "bundle_digest": row.bundle_digest}


@router.post("/tasks/{task_id}/integration-evidence")
async def record_manual_integration(
    task_id: UUID,
    body: ManualIntegrationRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to confirm integration")
    if body.evidence.get("operator_confirmation") is not True:
        raise HTTPException(
            422, "Manual integration requires explicit operator_confirmation evidence"
        )
    if len(json.dumps(body.evidence, ensure_ascii=False).encode()) > 64_000:
        raise HTTPException(413, "Integration evidence exceeds 64 KB")
    digest = hashlib.sha256(
        json.dumps(body.evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = await db.scalar(
        select(IntegrationEvidence)
        .where(
            IntegrationEvidence.project_id == task.project_id,
            IntegrationEvidence.stable_key == body.stable_key,
        )
        .with_for_update()
    )
    if existing:
        if (
            existing.evidence_digest != digest
            or existing.integration_revision != body.integration_revision
        ):
            raise HTTPException(
                409, "Integration stable key is already bound to different evidence"
            )
        return {
            "id": existing.id,
            "status": existing.status,
            "evidence_digest": existing.evidence_digest,
        }
    bundle = await db.scalar(
        select(ReviewBundle)
        .where(ReviewBundle.task_id == task_id, ReviewBundle.status == "published")
        .order_by(ReviewBundle.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if bundle is None or task.stage != "HUMAN_REVIEW":
        raise HTTPException(409, "A current reviewer-approved bundle is required")
    if body.expected_base_sha != bundle.base_sha:
        raise HTTPException(409, "Integration expected base does not match the approved bundle")
    repository = await db.scalar(
        select(Repository).where(Repository.id == bundle.repository_id).with_for_update()
    )
    if repository is None:
        raise HTTPException(404, "Repository not found")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"harnessa-integration:{repository.id}"},
    )
    current_base = await _manifest_workspace.current_base(repository)
    if current_base != body.expected_base_sha:
        raise HTTPException(409, "Repository base changed; refresh the integration preview")
    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(404, "Task project not found")
    integration = IntegrationEvidence(
        organization_id=project.organization_id,
        project_id=task.project_id,
        repository_id=repository.id,
        task_id=task.id,
        bundle_id=bundle.id,
        mode="local_manual",
        stable_key=body.stable_key,
        status="confirmed",
        evidence=body.evidence,
        evidence_digest=digest,
        integration_revision=body.integration_revision,
        confirmed_by=actor.id,
        confirmed_at=datetime.now(UTC),
    )
    db.add(integration)
    await db.flush()
    for kind in ("archive", "learning", "cleanup"):
        db.add(
            PostIntegrationStep(
                integration_evidence_id=integration.id, kind=kind, status="pending", result={}
            )
        )
    before = task.version
    task.stage = "LEARNING"
    task.version += 1
    db.add(
        TaskTransition(
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor.id,
            from_stage="HUMAN_REVIEW",
            to_stage="LEARNING",
            from_run_status=task.run_status,
            to_run_status=task.run_status,
            reason="Operator confirmed integration; durable post-integration steps are pending",
            command="integration_confirmed",
            version_before=before,
            version_after=task.version,
        )
    )
    await db.commit()
    return {"id": integration.id, "status": integration.status, "evidence_digest": digest}


@router.get("/tasks/{task_id}/integration-evidence", response_model=IntegrationEvidenceRead)
async def get_integration_evidence(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> IntegrationEvidenceRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    integration = await db.scalar(
        select(IntegrationEvidence)
        .where(IntegrationEvidence.task_id == task_id)
        .order_by(IntegrationEvidence.created_at.desc())
        .limit(1)
    )
    if integration is None:
        raise HTTPException(404, "Integration evidence not found")
    steps = list(
        (
            await db.scalars(
                select(PostIntegrationStep)
                .where(PostIntegrationStep.integration_evidence_id == integration.id)
                .order_by(PostIntegrationStep.kind)
            )
        ).all()
    )
    return IntegrationEvidenceRead(
        id=integration.id,
        status=integration.status,
        mode=integration.mode,
        stable_key=integration.stable_key,
        evidence=integration.evidence,
        evidence_digest=integration.evidence_digest,
        integration_revision=integration.integration_revision,
        steps=[
            PostIntegrationStepRead(
                kind=step.kind,
                status=step.status,
                attempts=step.attempts,
                result=step.result,
                last_error=step.last_error,
            )
            for step in steps
        ],
    )


@router.put("/integration-evidence/{integration_id}/steps/{kind}")
async def update_post_integration_step(
    integration_id: UUID,
    kind: str,
    body: PostIntegrationStepRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if kind not in {"archive", "learning", "cleanup"}:
        raise HTTPException(404, "Post-integration step not found")
    integration = await db.get(IntegrationEvidence, integration_id)
    if integration is None or integration.status != "confirmed":
        raise HTTPException(404, "Confirmed integration evidence not found")
    membership = await require_membership(db, actor, integration.project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required")
    step = await db.scalar(
        select(PostIntegrationStep)
        .where(
            PostIntegrationStep.integration_evidence_id == integration_id,
            PostIntegrationStep.kind == kind,
        )
        .with_for_update()
    )
    if step is None:
        raise HTTPException(404, "Post-integration step not found")
    link = await db.scalar(
        select(GitHubRepositoryLink).where(
            GitHubRepositoryLink.repository_id == integration.repository_id
        )
    )
    if (
        kind == "archive"
        and integration.mode == "github"
        and link is not None
        and link.archive_policy == "post_merge"
    ):
        if body.status == "succeeded":
            raise HTTPException(
                409,
                "Post-merge archive completion requires verified GitHub merge evidence",
            )
        if body.status == "pending":
            if step.status != "failed":
                raise HTTPException(409, "Only a failed maintenance archive can be retried")
            step.status = "pending"
            step.attempts = 0
            step.last_error = None
            step.result = {
                "outcome": "retry_requested",
                "reason": body.error or "operator_retry",
            }
        else:
            step.attempts += 1
            step.status = body.status
            step.result = body.result
            step.last_error = body.error
        await complete_task_if_ready(db, integration_id)
        await db.commit()
        return {"kind": step.kind, "status": step.status, "attempts": step.attempts}
    if body.status == "pending":
        raise HTTPException(422, "Pending is available only for a failed post-merge archive retry")
    step.status = body.status
    step.attempts += 1
    step.result = body.result
    step.last_error = body.error
    await complete_task_if_ready(db, integration_id)
    await db.commit()
    return {"kind": step.kind, "status": step.status, "attempts": step.attempts}
