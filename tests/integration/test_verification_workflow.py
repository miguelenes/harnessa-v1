from __future__ import annotations

import asyncio

import pytest
from harnessa.config import settings
from harnessa.db.orchestration import Run
from harnessa.db.review import ReviewBundle, VerificationExecution
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.services.scheduler import Scheduler
from harnessa.services.verification_workflow import VerificationDispatcher
from sqlalchemy import select

from tests.integration.test_specifications import (
    _create_ready_interview,
    _git,
    _specification_fixture,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_missing_successful_run_is_recorded_as_unverified(tmp_path, monkeypatch):
    project_id, repository_id, actor, _repository, _manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        task_id = interview.task_id
        source_revision = interview.base_sha
    scheduler = Scheduler(global_limit=1)
    job = await scheduler.claim("missing-successful-run", "test-fake-execution")
    assert job is not None and job.task_id == task_id
    await scheduler.complete(job, {"source_revision": source_revision})
    async with SessionLocal() as db:
        run = await db.scalar(select(Run).where(Run.task_id == task_id))
        assert run is not None
        run.status = "FAILED"
        await db.commit()

    assert await VerificationDispatcher(repository_sources={}).run_once() is True
    async with SessionLocal() as db:
        from harnessa.db.models import Task

        task = await db.get(Task, task_id)
        assert task is not None
        assert task.stage == "HUMAN_REVIEW"
        assert task.blocked_reason == "verification_unverified"


@pytest.mark.anyio
async def test_repository_source_mismatch_is_recorded_as_unverified(tmp_path, monkeypatch):
    project_id, repository_id, actor, _repository, _manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        task_id = interview.task_id
        source_revision = interview.base_sha
    scheduler = Scheduler(global_limit=1)
    job = await scheduler.claim("mismatched-verifier-source", "test-fake-execution")
    assert job is not None and job.task_id == task_id
    await scheduler.complete(job, {"source_revision": source_revision})
    mismatched_source = tmp_path / "different-source"
    mismatched_source.mkdir()

    dispatcher = VerificationDispatcher(repository_sources={str(project_id): mismatched_source})
    assert await dispatcher.run_once() is True
    async with SessionLocal() as db:
        evidence = await db.scalar(
            select(VerificationExecution).where(VerificationExecution.task_id == task_id)
        )
        bundle = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        assert evidence is not None and evidence.outcome == "unverified"
        assert "do not match" in evidence.result["reason"]
        assert bundle is not None and bundle.bundle["verification"]["outcome"] == "unverified"


@pytest.mark.anyio
async def test_unavailable_repository_source_finishes_as_visible_unverified_review(
    tmp_path, monkeypatch
):
    project_id, repository_id, actor, _repository, _manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        task_id = interview.task_id
        source_revision = interview.base_sha

    scheduler = Scheduler(global_limit=1)
    job = await scheduler.claim("verification-fixture", "test-fake-execution")
    assert job is not None and job.task_id == task_id
    await scheduler.complete(job, {"source_revision": source_revision})

    dispatcher = VerificationDispatcher(repository_sources={})
    assert await dispatcher.run_once() is True

    async with SessionLocal() as db:
        evidence = await db.scalar(
            select(VerificationExecution).where(VerificationExecution.task_id == task_id)
        )
        bundle = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        from harnessa.db.models import Task

        task = await db.get(Task, task_id)
        assert evidence is not None and evidence.outcome == "unverified"
        assert evidence.result["criteria"]
        assert bundle is not None
        assert bundle.bundle["openspec_sync_preview"]["archive_preview_ready"] is False
        assert task is not None and task.stage == "HUMAN_REVIEW"
        assert task.blocked_reason == "verification_unverified"


@pytest.mark.anyio
async def test_missing_approved_image_is_unverified_and_still_publishes_review_evidence(
    tmp_path, monkeypatch
):
    project_id, repository_id, actor, repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        task_id = interview.task_id
        source_revision = interview.base_sha

    scheduler = Scheduler(global_limit=1)
    job = await scheduler.claim("verification-image-fixture", "test-fake-execution")
    assert job is not None and job.task_id == task_id
    await scheduler.complete(job, {"source_revision": source_revision})
    source_path = await manager.source_path(repository)
    dispatcher = VerificationDispatcher(repository_sources={str(project_id): source_path})

    assert await dispatcher.run_once() is True

    async with SessionLocal() as db:
        evidence = await db.scalar(
            select(VerificationExecution).where(VerificationExecution.task_id == task_id)
        )
        bundle = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        assert evidence is not None and evidence.outcome == "unverified"
        assert "commands" in evidence.result
        assert bundle is not None
        assert bundle.bundle["openspec_sync_preview"]["archive_preview_ready"] is True
        assert bundle.bundle["openspec_sync_preview"]["final_head_verified"] is True
        assert (
            bundle.bundle["openspec_sync_preview"]["proposed_final_review_head"] == bundle.head_sha
        )
        assert evidence.source_revision == bundle.head_sha
        assert evidence.result["implementation_head_sha"] == source_revision
        assert bundle.head_sha != source_revision
        source = await manager.source_path(repository)
        assert (
            await asyncio.to_thread(
                _git, source, "cat-file", "-e", f"{bundle.head_sha}:openspec/specs/access/spec.md"
            )
            == ""
        )
        assert "openspec/changes/archive/" in bundle.bundle["openspec_sync_preview"]["archive_diff"]
        assert bundle.bundle["verification"]["outcome"] == "unverified"


@pytest.mark.anyio
async def test_failed_verifier_evidence_returns_task_to_ready_with_review_bundle(
    tmp_path, monkeypatch
):
    from harnessa.adapters.verification import (
        CommandEvidence,
        CriterionEvidence,
        VerificationResult,
    )
    from harnessa.services import verification_workflow

    project_id, repository_id, actor, repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        task_id = interview.task_id
        source_revision = interview.base_sha

    class FailedRunner:
        async def run(self, manifest, *, repository_path, source_revision, protected_fixtures):
            return VerificationResult(
                source_revision=source_revision,
                manifest_revision=1,
                manifest_digest="a" * 64,
                outcome="failed",
                commands=(
                    CommandEvidence(
                        command_id="fixture-check",
                        image="localhost/fixture@sha256:" + "0" * 64,
                        exit_code=2,
                        timed_out=False,
                        stdout="check failed",
                        stderr="",
                        duration_ms=2,
                        outcome="failed",
                    ),
                ),
                criteria=(
                    CriterionEvidence(
                        criterion_id="dispatch-ready",
                        outcome="failed",
                        summary="The approved check failed.",
                        command_ids=("fixture-check",),
                    ),
                    CriterionEvidence(
                        criterion_id="operator-review",
                        outcome="manual_review_required",
                        summary="This criterion requires human review.",
                        command_ids=(),
                    ),
                ),
                content_hash="b" * 64,
            )

    monkeypatch.setattr(
        verification_workflow,
        "VerificationRunner",
        lambda executor, max_total_seconds: FailedRunner(),
    )
    scheduler = Scheduler(global_limit=1)
    job = await scheduler.claim("verification-repair-fixture", "test-fake-execution")
    assert job is not None and job.task_id == task_id
    await scheduler.complete(job, {"source_revision": source_revision})
    source_path = await manager.source_path(repository)
    dispatcher = VerificationDispatcher(repository_sources={str(project_id): source_path})

    assert await dispatcher.run_once() is True

    async with SessionLocal() as db:
        evidence = await db.scalar(
            select(VerificationExecution).where(VerificationExecution.task_id == task_id)
        )
        bundle = await db.scalar(select(ReviewBundle).where(ReviewBundle.task_id == task_id))
        from harnessa.db.models import Task

        task = await db.get(Task, task_id)
        assert evidence is not None and evidence.outcome == "failed"
        assert bundle is not None and bundle.bundle["verification"]["outcome"] == "failed"
        assert task is not None and task.stage == "READY"
        assert task.run_status == "QUEUED"
        assert task.blocked_reason == "verification_repair"
