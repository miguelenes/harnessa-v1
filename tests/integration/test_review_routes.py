from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import HTTPException
from harnessa.api import review_routes
from harnessa.config import settings
from harnessa.contracts import (
    ManualIntegrationRequest,
    PostIntegrationStepRequest,
    ReviewDecisionRequest,
    VerificationManifestApprovalRequest,
)
from harnessa.db.model_gateway import ModelBudgetPolicy
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import Run
from harnessa.db.review import ReviewBundle, VerificationExecution, VerificationManifestRevision
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.services.model_gateway import budget_policy_digest
from sqlalchemy import select

from tests.integration.test_specifications import (
    _create_ready_interview,
    _specification_fixture,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_manifest_approval_is_revision_bound_and_fixtures_are_immutable(
    tmp_path, monkeypatch
):
    _project_id, repository_id, actor, _repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(review_routes, "_manifest_workspace", manager)
    with pytest.raises(HTTPException) as missing_repository:
        async with SessionLocal() as missing_db:
            await review_routes.latest_verification_manifest(
                "00000000-0000-0000-0000-000000000000", actor, missing_db
            )
    assert missing_repository.value.status_code == 404
    async with SessionLocal() as db:
        repository = await db.get(Repository, repository_id)
        assert repository is not None
        base_sha = await manager.current_base(repository)
        request = VerificationManifestApprovalRequest(
            source_path="verification.json",
            source_revision=base_sha,
            protected_fixtures={
                "expected/answer.txt": base64.b64encode(b"secret fixture").decode()
            },
        )
        approved = await review_routes.approve_verification_manifest(
            repository_id, request, actor, db
        )
        assert approved.revision == 1
        assert "secret fixture" not in str(approved.model_dump())
        replay = await review_routes.approve_verification_manifest(
            repository_id, request, actor, db
        )
        assert replay.id == approved.id
        latest = await review_routes.latest_verification_manifest(repository_id, actor, db)
        assert latest.id == approved.id
        with pytest.raises(HTTPException) as changed_fixtures:
            await review_routes.approve_verification_manifest(
                repository_id,
                request.model_copy(update={"protected_fixtures": {}}),
                actor,
                db,
            )
        assert changed_fixtures.value.status_code == 409
        with pytest.raises(HTTPException) as stale_base:
            await review_routes.approve_verification_manifest(
                repository_id,
                request.model_copy(update={"source_revision": "a" * 40}),
                actor,
                db,
            )
        assert stale_base.value.status_code == 409


@pytest.mark.anyio
async def test_review_bundle_approval_manual_integration_and_recoverable_steps(
    tmp_path, monkeypatch
):
    project_id, repository_id, actor, _repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    monkeypatch.setattr(review_routes, "_manifest_workspace", manager)
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as missing_bundle:
            await review_routes.get_review_bundle("00000000-0000-0000-0000-000000000000", actor, db)
        assert missing_bundle.value.status_code == 404
        interview = await db.get(SpecificationInterview, interview_id)
        repository_row = await db.get(Repository, repository_id)
        project = await db.get(Project, project_id)
        assert interview is not None and repository_row is not None and project is not None
        task = await db.get(Task, interview.task_id)
        policy = await db.scalar(
            select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id)
        )
        manifest = await db.scalar(
            select(VerificationManifestRevision).where(
                VerificationManifestRevision.repository_id == repository_id
            )
        )
        assert task is not None and policy is not None and manifest is not None
        source_revision = interview.base_sha
        task.stage = "HUMAN_REVIEW"
        task.run_status = "SUCCEEDED"
        run = Run(
            task_id=task.id,
            project_id=project_id,
            attempt=1,
            status="SUCCEEDED",
            fencing_token=1,
            adapter_name="fixture",
            source_revision=source_revision,
            base_sha=interview.base_sha,
            specification_hash=interview.artifact_hash,
            specification_revision=interview.artifact_revision,
            specification_commit_sha=interview.head_sha,
            policy_revision=interview.policy_revision,
            budget_policy_digest=budget_policy_digest(policy),
            harness_revision=settings.acp_harness_revision,
            harness_digest=hashlib.sha256(b"").hexdigest(),
            agent_image=settings.acp_agent_image,
            verification_manifest_revision_id=manifest.id,
        )
        db.add(run)
        await db.flush()
        execution = VerificationExecution(
            organization_id=project.organization_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task.id,
            run_id=run.id,
            manifest_revision_id=manifest.id,
            attempt=1,
            fencing_token=1,
            source_revision=source_revision,
            outcome="passed",
            result={"criteria": [{"id": "dispatch-ready", "outcome": "passed"}]},
            content_hash=hashlib.sha256(b"evidence").hexdigest(),
        )
        db.add(execution)
        await db.flush()
        digest = hashlib.sha256(b"review-bundle").hexdigest()
        bundle = ReviewBundle(
            organization_id=project.organization_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task.id,
            run_id=run.id,
            verification_execution_id=execution.id,
            fencing_token=1,
            base_sha=interview.base_sha,
            head_sha=source_revision,
            specification_hash=interview.artifact_hash,
            specification_revision=interview.artifact_revision,
            harness_revision=settings.acp_harness_revision,
            manifest_digest=manifest.manifest_digest,
            bundle_digest=digest,
            artifact_digest=hashlib.sha256(b"bundle-artifact").hexdigest(),
            bundle={
                "implementation_head_sha": source_revision,
                "openspec_sync_preview": {
                    "archive_preview_ready": True,
                    "final_head_verified": True,
                    "implementation_head_sha": source_revision,
                    "proposed_final_review_head": source_revision,
                    "archive_diff": "",
                    "diff_sha256": hashlib.sha256(b"").hexdigest(),
                },
            },
            status="prepared",
        )
        db.add(bundle)
        await db.commit()
        await db.refresh(bundle)
        task_id = task.id

    async with SessionLocal() as db:
        approved = await review_routes.approve_verification_manifest(
            repository_id,
            VerificationManifestApprovalRequest(
                source_path="verification.json", source_revision=source_revision
            ),
            actor,
            db,
        )
        latest = await review_routes.latest_verification_manifest(repository_id, actor, db)
        assert approved.id == latest.id
        with pytest.raises(HTTPException) as unsafe_fixture:
            await review_routes.approve_verification_manifest(
                repository_id,
                VerificationManifestApprovalRequest(
                    source_path="verification.json",
                    source_revision=source_revision,
                    protected_fixtures={"../secret": "cHJpdmF0ZQ=="},
                ),
                actor,
                db,
            )
        assert unsafe_fixture.value.status_code == 422

        published = await review_routes.get_review_bundle(task_id, actor, db)
        assert published["status"] == "prepared"
        assert published["publication"]["provider"] == "local"
        bundle_row = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        assert bundle_row is not None
        bundle_row.bundle = {"openspec_sync_preview": {"archive_preview_ready": False}}
        await db.flush()
        with pytest.raises(HTTPException) as unavailable_archive:
            await review_routes.decide_review_bundle(
                task_id,
                ReviewDecisionRequest(
                    decision="comment",
                    rationale="Must be blocked without archive preview.",
                    idempotency_key="m9-archive-blocked",
                    bundle_digest=digest,
                ),
                actor,
                db,
            )
        assert unavailable_archive.value.status_code == 409
        bundle_row.status = "prepared"
        bundle_row.invalidated_reason = None
        bundle_row.bundle = {
            "implementation_head_sha": source_revision,
            "openspec_sync_preview": {
                "archive_preview_ready": True,
                "final_head_verified": True,
                "implementation_head_sha": source_revision,
                "proposed_final_review_head": source_revision,
                "archive_diff": "",
                "diff_sha256": hashlib.sha256(b"").hexdigest(),
            },
        }
        await db.flush()
        comment = await review_routes.decide_review_bundle(
            task_id,
            ReviewDecisionRequest(
                decision="comment",
                rationale="Bundle was inspected.",
                idempotency_key="m9-comment",
                bundle_digest=digest,
            ),
            actor,
            db,
        )
        assert comment["decision"] == "comment"
        with pytest.raises(HTTPException) as reused_key:
            await review_routes.decide_review_bundle(
                task_id,
                ReviewDecisionRequest(
                    decision="comment",
                    rationale="Changed rationale",
                    idempotency_key="m9-comment",
                    bundle_digest=digest,
                ),
                actor,
                db,
            )
        assert reused_key.value.status_code == 409
        with pytest.raises(HTTPException) as unknown_bundle:
            await review_routes.decide_review_bundle(
                task_id,
                ReviewDecisionRequest(
                    decision="approve",
                    rationale="Unknown bundle",
                    idempotency_key="m9-unknown-bundle",
                    bundle_digest="0" * 64,
                ),
                actor,
                db,
            )
        assert unknown_bundle.value.status_code == 404
        decision = await review_routes.decide_review_bundle(
            task_id,
            ReviewDecisionRequest(
                decision="approve",
                rationale="All current command evidence passed.",
                idempotency_key="m9-approve",
                bundle_digest=digest,
            ),
            actor,
            db,
        )
        assert decision["decision"] == "approve"
        replay = await review_routes.decide_review_bundle(
            task_id,
            ReviewDecisionRequest(
                decision="approve",
                rationale="All current command evidence passed.",
                idempotency_key="m9-approve",
                bundle_digest=digest,
            ),
            actor,
            db,
        )
        assert replay["id"] == decision["id"]
        with pytest.raises(HTTPException) as no_confirmation:
            await review_routes.record_manual_integration(
                task_id,
                ManualIntegrationRequest(
                    expected_base_sha=source_revision,
                    stable_key="m9-missing-confirmation",
                    integration_revision=source_revision,
                    evidence={},
                ),
                actor,
                db,
            )
        assert no_confirmation.value.status_code == 422
        with pytest.raises(HTTPException) as oversized_evidence:
            await review_routes.record_manual_integration(
                task_id,
                ManualIntegrationRequest(
                    expected_base_sha=source_revision,
                    stable_key="m9-oversized-evidence",
                    integration_revision=source_revision,
                    evidence={"operator_confirmation": True, "details": "x" * 65_000},
                ),
                actor,
                db,
            )
        assert oversized_evidence.value.status_code == 413
        with pytest.raises(HTTPException) as stale_base:
            await review_routes.record_manual_integration(
                task_id,
                ManualIntegrationRequest(
                    expected_base_sha="b" * 40,
                    stable_key="m9-stale-base",
                    integration_revision=source_revision,
                    evidence={"operator_confirmation": True},
                ),
                actor,
                db,
            )
        assert stale_base.value.status_code == 409
        integration = await review_routes.record_manual_integration(
            task_id,
            ManualIntegrationRequest(
                expected_base_sha=source_revision,
                stable_key="m9-local-integration",
                integration_revision=source_revision,
                evidence={"operator_confirmation": True, "mode": "disposable fixture"},
            ),
            actor,
            db,
        )
        assert integration["status"] == "confirmed"
        task = await db.get(Task, task_id)
        assert task is not None and task.stage == "LEARNING"
        integration_replay = await review_routes.record_manual_integration(
            task_id,
            ManualIntegrationRequest(
                expected_base_sha=source_revision,
                stable_key="m9-local-integration",
                integration_revision=source_revision,
                evidence={"operator_confirmation": True, "mode": "disposable fixture"},
            ),
            actor,
            db,
        )
        assert integration_replay["id"] == integration["id"]
        with pytest.raises(HTTPException) as conflicting_evidence:
            await review_routes.record_manual_integration(
                task_id,
                ManualIntegrationRequest(
                    expected_base_sha=source_revision,
                    stable_key="m9-local-integration",
                    integration_revision=source_revision,
                    evidence={"operator_confirmation": True, "mode": "changed"},
                ),
                actor,
                db,
            )
        assert conflicting_evidence.value.status_code == 409
        for kind in ("archive", "learning", "cleanup"):
            step = await review_routes.update_post_integration_step(
                integration["id"],
                kind,
                PostIntegrationStepRequest(status="succeeded", result={"fixture": True}),
                actor,
                db,
            )
            assert step["status"] == "succeeded"
        task = await db.get(Task, task_id)
        assert task is not None and task.stage == "DONE"
        with pytest.raises(HTTPException) as unknown_step:
            await review_routes.update_post_integration_step(
                integration["id"],
                "unknown",
                PostIntegrationStepRequest(status="succeeded"),
                actor,
                db,
            )
        assert unknown_step.value.status_code == 404
        with pytest.raises(HTTPException) as missing_integration:
            await review_routes.update_post_integration_step(
                "00000000-0000-0000-0000-000000000000",
                "archive",
                PostIntegrationStepRequest(status="succeeded"),
                actor,
                db,
            )
        assert missing_integration.value.status_code == 404

        task = await db.get(Task, task_id)
        bundle = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        assert task is not None and bundle is not None
        task.stage = "HUMAN_REVIEW"
        task.run_status = "SUCCEEDED"
        bundle.status = "prepared"
        await db.flush()
        requested = await review_routes.decide_review_bundle(
            task_id,
            ReviewDecisionRequest(
                decision="request_changes",
                rationale="The operator asked for a revised patch.",
                idempotency_key="m9-request-changes",
                bundle_digest=digest,
            ),
            actor,
            db,
        )
        assert requested["decision"] == "request_changes"
        assert task.stage == "READY" and bundle.status == "invalidated"
