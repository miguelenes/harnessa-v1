"""Durable finalization of archive, optional learning, and cleanup outcomes."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
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
            status = "skipped" if result.get("outcome") == "skipped" else "succeeded"
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
    async with session_factory() as db:
        integration = await db.get(IntegrationEvidence, integration_id)
        if integration is None or integration.status != "confirmed":
            raise SpecificationWorkspaceError("Confirmed integration evidence is unavailable")
        bundle = await db.get(ReviewBundle, integration.bundle_id)
        if bundle is None:
            raise SpecificationWorkspaceError("Review bundle is unavailable")
        if kind == "archive":
            return _archive_result(integration, bundle)
        if kind == "learning":
            # No project-scoped learning hook is configured in this milestone.
            # Record the optional hook's terminal outcome without blocking DONE.
            return {
                "outcome": "skipped",
                "reason": "no_learning_hook_configured",
                "optional": True,
            }
        if kind != "cleanup":
            raise SpecificationWorkspaceError("Unknown post-integration step")
        repository = await db.get(Repository, integration.repository_id)
        interview = await db.scalar(
            select(SpecificationInterview).where(
                SpecificationInterview.task_id == integration.task_id
            )
        )
        if repository is None or interview is None:
            raise SpecificationWorkspaceError("Integrated repository or interview is unavailable")
        sync_preview = bundle.bundle.get("openspec_sync_preview")
        if not isinstance(sync_preview, dict):
            raise SpecificationWorkspaceError("Review bundle archive provenance is missing")
        reference = sync_preview.get("review_candidate_ref")
        if not isinstance(reference, str):
            raise SpecificationWorkspaceError("Review candidate reference is missing")
        reference_result = await workspace_manager.remove_candidate_ref(
            repository, reference, bundle.head_sha
        )
        workspace_removed = await workspace_manager.remove_interview_workspace(interview.id)
        return {
            "outcome": "succeeded",
            "review_candidate_ref": reference_result,
            "interview_workspace": "removed" if workspace_removed else "already_absent",
        }


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
        if (
            integration.evidence.get("github_head_sha") != bundle.head_sha
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
    task = await db.scalar(
        select(Task).where(Task.id == integration.task_id).with_for_update()
    )
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
