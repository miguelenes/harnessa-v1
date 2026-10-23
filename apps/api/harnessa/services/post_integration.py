"""Durable finalization of archive, optional learning, and cleanup outcomes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.db.harness import HarnessExperience, HarnessExperiment
from harnessa.db.models import Repository, Task
from harnessa.db.orchestration import TaskTransition
from harnessa.db.review import (
    IntegrationEvidence,
    PostIntegrationStep,
    ReviewBundle,
)
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("harnessa.post_integration")
_MAX_STEP_ATTEMPTS = 12


async def process_post_integration_steps(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    workspace_manager: SpecificationWorkspaceManager | None = None,
    limit: int = 20,
) -> int:
    """Claim and execute bounded, idempotent post-integration work."""
    if limit < 1 or limit > 100:
        raise ValueError("Post-integration step limit must be between 1 and 100")
    manager = workspace_manager or SpecificationWorkspaceManager()
    now = datetime.now(UTC)
    claimed: list[tuple[UUID, UUID, str]] = []
    async with session_factory() as db:
        rows = (
            await db.scalars(
                select(PostIntegrationStep)
                .where(
                    or_(
                        PostIntegrationStep.status == "pending",
                        and_(
                            PostIntegrationStep.status == "failed",
                            PostIntegrationStep.attempts < _MAX_STEP_ATTEMPTS,
                            PostIntegrationStep.updated_at <= now - timedelta(seconds=30),
                        ),
                        and_(
                            PostIntegrationStep.status == "running",
                            PostIntegrationStep.updated_at <= now - timedelta(seconds=120),
                        ),
                    )
                )
                .order_by(PostIntegrationStep.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
        for row in rows:
            row.status = "running"
            row.attempts += 1
            row.last_error = None
            claimed.append((row.id, row.integration_evidence_id, row.kind))
        await db.commit()

    for step_id, integration_id, kind in claimed:
        try:
            result = await _execute_step(
                integration_id,
                kind,
                session_factory=session_factory,
                workspace_manager=manager,
            )
            status = {
                "skipped": "skipped",
                "awaiting_merge": "awaiting_merge",
            }.get(str(result.get("outcome")), "succeeded")
            error = None
        except Exception as exc:
            status = "failed"
            error = f"Post-integration {kind} step failed ({type(exc).__name__})"
            result = {"outcome": "retryable_failure", "error_type": type(exc).__name__}
            logger.warning(
                "Post-integration %s step failed for integration %s",
                kind,
                integration_id,
                exc_info=True,
            )
        async with session_factory() as db:
            step = await db.scalar(
                select(PostIntegrationStep)
                .where(PostIntegrationStep.id == step_id)
                .with_for_update()
            )
            if step is None or step.status != "running":
                continue
            step.status = status
            step.result = result
            step.last_error = error
            await complete_task_if_ready(db, integration_id)
            await db.commit()
    return len(claimed)


async def _execute_step(
    integration_id: UUID,
    kind: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    workspace_manager: SpecificationWorkspaceManager,
) -> dict[str, Any]:
    archive_result: dict[str, Any] | None = None
    archive_policy = "pre_merge"
    repository: Repository | None = None
    interview_id: UUID | None = None
    candidate_ref: str | None = None
    async with session_factory() as db:
        integration = await db.get(IntegrationEvidence, integration_id)
        if integration is None or integration.status != "confirmed":
            raise SpecificationWorkspaceError("Confirmed integration evidence is unavailable")
        bundle = await db.get(ReviewBundle, integration.bundle_id)
        if bundle is None:
            raise SpecificationWorkspaceError("Review bundle is unavailable")
        if kind == "archive":
            archive_result = _archive_result(integration, bundle)
            if integration.mode == "github":
                from harnessa.db.github import GitHubRepositoryLink

                link = await db.scalar(
                    select(GitHubRepositoryLink).where(
                        GitHubRepositoryLink.repository_id == integration.repository_id,
                        GitHubRepositoryLink.opted_in.is_(True),
                        GitHubRepositoryLink.setup_status == "ready",
                    )
                )
                if link is None:
                    raise SpecificationWorkspaceError("Opted-in GitHub repository is unavailable")
                archive_policy = link.archive_policy
        elif kind == "learning":
            hook = bundle.bundle.get("harness_adaptation_hook")
            if not isinstance(hook, dict) or hook.get("triggered") is not True:
                return {
                    "outcome": "skipped",
                    "reason": "no_completed_harness_experiment",
                    "optional": True,
                }
            try:
                experiment_id = UUID(str(hook["experiment_id"]))
                cue = str(hook["cue"]).strip()
                lesson = str(hook["lesson"]).strip()
                evidence_refs = hook["evidence_refs"]
            except (KeyError, ValueError, TypeError) as exc:
                raise SpecificationWorkspaceError(
                    "Completed harness learning evidence is malformed"
                ) from exc
            if not cue or len(cue) > 1000 or not lesson or len(lesson) > 2000:
                raise SpecificationWorkspaceError(
                    "Harness learning cue or lesson is outside its bound"
                )
            if (
                not isinstance(evidence_refs, list)
                or not evidence_refs
                or any(
                    not isinstance(ref, dict)
                    or not isinstance(ref.get("digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", ref["digest"])
                    for ref in evidence_refs
                )
            ):
                raise SpecificationWorkspaceError(
                    "Harness learning evidence references are invalid"
                )
            experiment = await db.scalar(
                select(HarnessExperiment).where(
                    HarnessExperiment.id == experiment_id,
                    HarnessExperiment.task_id == integration.task_id,
                    HarnessExperiment.project_id == integration.project_id,
                    HarnessExperiment.status == "succeeded",
                )
            )
            repository = await db.get(Repository, integration.repository_id)
            if experiment is None or repository is None:
                raise SpecificationWorkspaceError(
                    "Successful experiment or repository scope is unavailable"
                )
            key = hashlib.sha256(f"{experiment.id}:{cue}".encode()).hexdigest()[:32]
            existing = await db.scalar(
                select(HarnessExperience).where(
                    HarnessExperience.project_id == integration.project_id,
                    HarnessExperience.experience_key == key,
                    HarnessExperience.version == 1,
                )
            )
            if existing:
                return {
                    "outcome": "succeeded",
                    "experience_id": str(existing.id),
                    "status": existing.status,
                    "idempotent_replay": True,
                }
            payload = {
                "cue": cue,
                "lesson": lesson,
                "evidence_refs": evidence_refs,
                "repository_scope": repository.target_id,
                "domain_scope": str(hook.get("domain_scope", "repository")),
            }
            candidate_digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            candidate = HarnessExperience(
                organization_id=integration.organization_id,
                project_id=integration.project_id,
                source_task_id=integration.task_id,
                source_experiment_id=experiment.id,
                experience_key=key,
                version=1,
                status="candidate",
                repository_scope=repository.target_id,
                domain_scope=str(hook.get("domain_scope", "repository"))[:120],
                cue=cue,
                lesson=lesson,
                evidence_refs=evidence_refs,
                candidate_digest=candidate_digest,
                conflict_key=str(hook.get("conflict_key", key))[:120],
                expiry_at=datetime.now(UTC) + timedelta(days=180),
                policy_revision=experiment.policy_revision,
            )
            db.add(candidate)
            await db.flush()
            await db.commit()
            return {
                "outcome": "succeeded",
                "experience_id": str(candidate.id),
                "status": "candidate",
                "automatically_activated": False,
            }
        elif kind == "cleanup":
            repository = await db.get(Repository, integration.repository_id)
            interview = await db.scalar(
                select(SpecificationInterview).where(
                    SpecificationInterview.task_id == integration.task_id
                )
            )
            if repository is None or interview is None:
                raise SpecificationWorkspaceError(
                    "Integrated repository or interview is unavailable"
                )
            sync_preview = bundle.bundle.get("openspec_sync_preview")
            if not isinstance(sync_preview, dict):
                raise SpecificationWorkspaceError("Review bundle archive provenance is missing")
            reference = sync_preview.get("review_candidate_ref")
            if not isinstance(reference, str):
                raise SpecificationWorkspaceError("Review candidate reference is missing")
            candidate_ref = reference
            interview_id = interview.id
        else:
            raise SpecificationWorkspaceError("Unknown post-integration step")

    if kind == "archive" and archive_result is not None:
        if archive_policy == "post_merge":
            from harnessa.adapters.github_effects import GitHubEffectReconciler

            published = await GitHubEffectReconciler(
                session_factory, workspace_manager=workspace_manager
            ).publish_archive_maintenance(integration_id)
            return {**archive_result, **published}
        return archive_result
    if kind == "cleanup" and repository is not None and interview_id is not None:
        if candidate_ref is None:
            raise SpecificationWorkspaceError("Review candidate reference is missing")
        reference_result = await workspace_manager.remove_candidate_ref(
            repository, candidate_ref, bundle.head_sha
        )
        workspace_removed = await workspace_manager.remove_interview_workspace(interview_id)
        return {
            "outcome": "succeeded",
            "review_candidate_ref": reference_result,
            "interview_workspace": "removed" if workspace_removed else "already_absent",
        }
    raise SpecificationWorkspaceError("Post-integration step could not be executed")


def _archive_result(integration: IntegrationEvidence, bundle: ReviewBundle) -> dict[str, Any]:
    preview = bundle.bundle.get("openspec_sync_preview")
    if not isinstance(preview, dict):
        raise SpecificationWorkspaceError("Review bundle archive provenance is missing")
    archive_diff = preview.get("archive_diff")
    digest = preview.get("diff_sha256")
    if (
        preview.get("archive_preview_ready") is not True
        or preview.get("final_head_verified") is not True
        or preview.get("proposed_final_review_head") != bundle.head_sha
        or not isinstance(archive_diff, str)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or hashlib.sha256(archive_diff.encode()).hexdigest() != digest
    ):
        raise SpecificationWorkspaceError("Pre-merge archive evidence is stale or invalid")
    if integration.mode == "github":
        merge_sha = integration.evidence.get("merge_commit_sha")
        verified_source_sha = integration.evidence.get("verified_source_head_sha")
        published_github_sha = integration.evidence.get("github_head_sha")
        published_branch_sha = integration.evidence.get("published_branch_sha")
        if (
            verified_source_sha != bundle.head_sha
            or not isinstance(published_github_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", published_github_sha)
            or published_github_sha != published_branch_sha
            or not isinstance(merge_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", merge_sha)
        ):
            raise SpecificationWorkspaceError("Verified GitHub merge evidence is incomplete")
    else:
        merge_sha = integration.integration_revision
        if integration.evidence.get("operator_confirmation") is not True:
            raise SpecificationWorkspaceError("Manual integration lacks operator confirmation")
    return {
        "outcome": "succeeded",
        "policy": "pre_merge_archive",
        "archive_diff_sha256": digest,
        "review_head_sha": bundle.head_sha,
        "integration_revision": merge_sha,
        **(
            {"github_head_sha": integration.evidence["github_head_sha"]}
            if integration.mode == "github"
            else {}
        ),
    }


async def complete_task_if_ready(db: AsyncSession, integration_id: UUID) -> bool:
    integration = await db.get(IntegrationEvidence, integration_id)
    if integration is None:
        return False
    steps = (
        await db.scalars(
            select(PostIntegrationStep).where(
                PostIntegrationStep.integration_evidence_id == integration_id
            )
        )
    ).all()
    by_kind = {step.kind: step.status for step in steps}
    if (
        by_kind.get("archive") != "succeeded"
        or by_kind.get("cleanup") != "succeeded"
        or by_kind.get("learning") not in {"succeeded", "failed", "skipped"}
    ):
        return False
    task = await db.scalar(select(Task).where(Task.id == integration.task_id).with_for_update())
    if task is None or task.stage != "LEARNING":
        return False
    before = task.version
    task.stage = "DONE"
    task.version += 1
    db.add(
        TaskTransition(
            task_id=task.id,
            project_id=task.project_id,
            actor_id=None,
            from_stage="LEARNING",
            to_stage="DONE",
            from_run_status=task.run_status,
            to_run_status=task.run_status,
            reason="Archive and cleanup succeeded; optional learning reached a terminal outcome",
            command="post_integration_complete",
            command_key=integration.stable_key,
            version_before=before,
            version_after=task.version,
        )
    )
    return True
