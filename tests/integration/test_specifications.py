from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from harnessa.adapters.acp_execution import _latest_specification_context, _task_prompt
from harnessa.adapters.specification_architect import ArchitectDraft
from harnessa.adapters.specification_workspace import SpecificationWorkspaceManager
from harnessa.adapters.verification import VerificationManifest
from harnessa.config import settings
from harnessa.contracts import ModelProtocol, RunStatus, TaskStage
from harnessa.db.model_gateway import (
    AliasDeployment,
    ModelAlias,
    ModelBudgetPolicy,
    ModelDeployment,
    ModelPool,
    PoolAlias,
    ProviderAccount,
)
from harnessa.db.models import (
    Membership,
    Organization,
    Project,
    ProjectMembership,
    Repository,
    Task,
    User,
)
from harnessa.db.orchestration import DomainEvent, Run, TaskLease
from harnessa.db.review import VerificationManifestRevision
from harnessa.db.session import SessionLocal
from harnessa.db.specification import (
    SpecificationAnswer,
    SpecificationArtifactRevision,
    SpecificationInterview,
    SpecificationQuestion,
    SpecificationReadinessEvaluation,
)
from harnessa.main import app
from harnessa.services import specifications as specification_service
from harnessa.services.scheduler import Scheduler
from harnessa.services.specifications import (
    _initial_bundle,
    answer_question,
    create_agent_question,
    create_interview,
    evaluate_readiness,
    generate_architect_draft,
    get_readiness,
    wait_for_agent_answer,
)
from pwdlib import PasswordHash
from sqlalchemy import delete, func, select

pytestmark = pytest.mark.integration


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


async def _specification_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID, User, Repository, SpecificationWorkspaceManager]:
    source = tmp_path / "repository"
    source.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "--quiet", "--initial-branch=main", str(source)], check=True
    )
    (source / "openspec/specs/access").mkdir(parents=True)
    (source / "openspec/specs/access/spec.md").write_text(
        "# Access\n\nCheck permissions before creating tasks.\n", encoding="utf-8"
    )
    (source / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "criteria": [
                    {
                        "id": "dispatch-ready",
                        "requirement": "The implementation passes the approved check.",
                        "scenario": "Run the repository verification command.",
                    },
                    {
                        "id": "operator-review",
                        "requirement": "An operator reviews the result.",
                        "scenario": "Review the recorded evidence.",
                        "manual_review": True,
                    },
                ],
                "commands": [
                    {
                        "id": "fixture-check",
                        "argv": ["true"],
                        "image": "localhost/fixture@sha256:" + "0" * 64,
                        "criteria": ["dispatch-ready"],
                        "timeout_seconds": 1,
                        "output_limit_bytes": 1024,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    await asyncio.to_thread(_git, source, "add", "--all")
    await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "initial source",
        ],
        check=True,
    )

    manager = SpecificationWorkspaceManager(root=tmp_path / "private-workspaces")
    monkeypatch.setattr("harnessa.services.specifications._workspace_manager", manager)
    monkeypatch.setattr(
        settings,
        "openspec_repository_sources_json",
        json.dumps({"fixture/specifications": str(source)}),
    )
    monkeypatch.setattr(settings, "acp_execution_enabled", True)
    monkeypatch.setattr(settings, "acp_model_broker_enabled", True)
    monkeypatch.setattr(settings, "model_gateway_url", "http://fixture-model-gateway.invalid")
    monkeypatch.setattr(settings, "acp_model_alias", "fixture-acp")
    async with SessionLocal() as db:
        organization = Organization(name="Specification Integration")
        user = User(
            email=f"spec-{source.name}-{UUID(int=1)}@example.test",
            hashed_password=PasswordHash.recommended().hash("fixture-password-long-enough"),
            is_active=True,
            is_superuser=True,
            is_verified=True,
        )
        db.add_all([organization, user])
        await db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role="administrator"))
        project = Project(
            organization_id=organization.id,
            name="Specification Project",
            slug="specification-project",
        )
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=user.id, role="administrator"))
        repository = Repository(
            project_id=project.id,
            target_id="fixture/specifications",
            display_name="Specification fixture",
            default_branch="main",
        )
        db.add(repository)
        await db.commit()
        await db.refresh(user)
        await db.refresh(repository)
        return project.id, repository.id, user, repository, manager


async def _create_ready_interview(
    project_id: UUID,
    repository_id: UUID,
    actor: User,
    idempotency_key: str = "specification-topic-create",
    expect_replayed: bool = False,
    topic: str = "Add a scoped task acceptance workflow",
) -> UUID:
    await _configure_acp_route(project_id)
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic=topic,
            idempotency_key=idempotency_key,
        )
        assert replayed is expect_replayed
        interview, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        assert evaluation.state == "READY"
        assert evaluation.score == 1
        repository = await db.get(Repository, repository_id)
        assert repository is not None
        source = await specification_service._workspace_manager.read_file_at_revision(
            repository, interview.base_sha, "verification.json"
        )
        manifest = VerificationManifest.model_validate({**json.loads(source), "revision": 1})
        existing_manifest = await db.scalar(
            select(VerificationManifestRevision).where(
                VerificationManifestRevision.repository_id == repository_id,
                VerificationManifestRevision.source_revision == interview.base_sha,
                VerificationManifestRevision.source_path == "verification.json",
            )
        )
        if existing_manifest is None:
            db.add(
                VerificationManifestRevision(
                    organization_id=interview.organization_id,
                    project_id=interview.project_id,
                    repository_id=repository_id,
                    revision=1,
                    source_path="verification.json",
                    source_revision=interview.base_sha,
                    source_digest=hashlib.sha256(source).hexdigest(),
                    manifest=manifest.model_dump(mode="json"),
                    manifest_digest=manifest.digest,
                    protected_fixtures={},
                    protected_fixture_digest=hashlib.sha256(b"{}").hexdigest(),
                    approved_by=actor.id,
                    approved_at=datetime.now(UTC),
                )
            )
            await db.commit()
        return interview.id


async def _configure_acp_route(
    project_id: UUID,
    *,
    tools: bool = True,
    cancellation: bool = True,
    context_window: int | None = None,
    budget_max_output_tokens: int = 16_384,
) -> None:
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        if await db.scalar(
            select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id)
        ):
            return
        pool = ModelPool(
            organization_id=project.organization_id,
            name="fixture-interactive",
            kind="interactive",
        )
        account = ProviderAccount(
            organization_id=project.organization_id,
            name="fixture-provider",
            provider="fixture",
            secret_ref="env:FIXTURE_MODEL_KEY",
            enabled=True,
        )
        alias = ModelAlias(
            organization_id=project.organization_id,
            name=settings.acp_model_alias or "fixture-acp",
            description="",
            enabled=True,
        )
        db.add_all([pool, account, alias])
        await db.flush()
        deployment = ModelDeployment(
            organization_id=project.organization_id,
            provider_account_id=account.id,
            name="fixture-acp-deployment",
            actual_model="fixture/model",
            proxy_model="fixture-acp-proxy",
            capabilities={
                "protocols": [ModelProtocol.CHAT_COMPLETIONS.value],
                "streaming": False,
                "tools": tools,
                "usage_reporting": True,
                "cancellation": cancellation,
            },
            context_window=(
                context_window
                if context_window is not None
                else settings.acp_model_max_request_bytes + settings.acp_model_max_output_tokens
            ),
            input_cost_per_million=Decimal(1),
            output_cost_per_million=Decimal(1),
            price_revision="fixture-1",
            enabled=True,
        )
        db.add(deployment)
        await db.flush()
        db.add_all(
            [
                AliasDeployment(alias_id=alias.id, deployment_id=deployment.id, priority=0),
                PoolAlias(pool_id=pool.id, alias_id=alias.id, priority=0),
                ModelBudgetPolicy(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    project_limit_usd=Decimal(10),
                    task_limit_usd=Decimal(5),
                    run_limit_usd=Decimal(2),
                    project_token_limit=2_000_000,
                    task_token_limit=1_000_000,
                    run_token_limit=500_000,
                    max_steps=100,
                    max_wall_seconds=3600,
                    max_output_tokens=budget_max_output_tokens,
                    max_attempts=3,
                ),
            ]
        )
        await db.commit()


@pytest.mark.anyio
async def test_openspec_archive_preview_updates_specs_in_a_disposable_review_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    async with SessionLocal() as db:
        from harnessa.db.specification import SpecificationInterview

        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        base_sha = interview.base_sha
        head_sha = interview.head_sha
        change_id = interview.change_id
    workspace = await manager.open(
        interview_id,
        repository,
        change_id=change_id,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    original_status = await asyncio.to_thread(
        _git, workspace.workspace_path, "status", "--porcelain"
    )

    preview = await manager.archive_preview(workspace)

    assert "openspec/changes/archive/" in preview["diff"]
    assert f"openspec/changes/{change_id}/" in preview["diff"]
    assert preview["command_output"]
    assert (
        await asyncio.to_thread(_git, workspace.workspace_path, "status", "--porcelain")
        == original_status
    )
    assert await manager.current_base(repository) == base_sha


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_when_required_model_route_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "acp_model_broker_enabled", False)

    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a route-gated task",
            idempotency_key="missing-model-route",
        )
        assert replayed is False
        interview, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert interview.readiness_state == "NOT_READY"
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_without_an_interactive_model_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a task that requires an interactive model pool",
            idempotency_key="missing-interactive-pool",
        )
        assert replayed is False
        interview, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert interview.readiness_state == "NOT_READY"
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_without_a_compatible_enabled_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    await _configure_acp_route(project_id, tools=False)
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a task that requires model tools",
            idempotency_key="incompatible-interactive-route",
        )
        assert replayed is False
        interview, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert interview.readiness_state == "NOT_READY"
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_when_route_cannot_fit_broker_request_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    await _configure_acp_route(
        project_id,
        context_window=(
            settings.acp_model_max_request_bytes + settings.acp_model_max_output_tokens - 1
        ),
    )
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a task with a bounded model context",
            idempotency_key="undersized-context-route",
        )
        assert replayed is False
        _, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_without_project_model_budget_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    await _configure_acp_route(project_id)
    async with SessionLocal() as db:
        await db.execute(
            delete(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id)
        )
        await db.commit()
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a task requiring configured model budgets",
            idempotency_key="missing-model-budget-policy",
        )
        assert replayed is False
        _, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_readiness_stays_in_specifying_when_budget_cannot_cover_output_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    await _configure_acp_route(
        project_id,
        budget_max_output_tokens=settings.acp_model_max_output_tokens - 1,
    )
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Create a task requiring a sufficient model output budget",
            idempotency_key="insufficient-model-output-budget",
        )
        assert replayed is False
        _, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        task = await db.get(Task, interview.task_id, populate_existing=True)

        assert evaluation.state == "NOT_READY"
        assert evaluation.gates["execution_capabilities"]["model_route_ready"] is False
        assert task is not None and task.stage == TaskStage.SPECIFYING.value


@pytest.mark.anyio
async def test_topic_replay_readiness_and_revisioned_answer_invalidate_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    interview_id = await _create_ready_interview(project_id, repository_id, actor)

    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Add a scoped task acceptance workflow",
            idempotency_key="specification-topic-create",
        )
        assert replayed is True
        question = await db.scalar(
            select(SpecificationQuestion).where(
                SpecificationQuestion.interview_id == interview_id,
                SpecificationQuestion.source == "interview",
            )
        )
        assert question is not None
        assert question.evidence
        assert all(item["untrusted_source_content"] for item in question.evidence)

        answer, changed = await answer_question(
            db,
            interview_id=interview.id,
            question_id=question.id,
            actor=actor,
            expected_revision=1,
            answer="Add an explicit authorization denial scenario.",
            idempotency_key="specification-answer-one",
            changes_scope=False,
        )
        assert answer.provenance["actor_id"] == str(actor.id)
        assert changed.artifact_revision == 2
        assert changed.readiness_state == "STALE"
        task = await db.get(Task, changed.task_id, populate_existing=True)
        assert task is not None and task.stage == TaskStage.SPECIFYING.value

        replay, _ = await answer_question(
            db,
            interview_id=interview.id,
            question_id=question.id,
            actor=actor,
            expected_revision=1,
            answer="Add an explicit authorization denial scenario.",
            idempotency_key="specification-answer-one",
            changes_scope=False,
        )
        assert replay.id == answer.id
        with pytest.raises(HTTPException) as stale_answer:
            await answer_question(
                db,
                interview_id=interview.id,
                question_id=question.id,
                actor=actor,
                expected_revision=1,
                answer="Another answer to the stale revision.",
                idempotency_key="specification-answer-stale",
                changes_scope=False,
            )
        assert stale_answer.value.status_code == 409

        revised, evaluation = await evaluate_readiness(db, interview_id=interview.id, actor=actor)
        assert evaluation.state == "READY"
        assert revised.artifact_revision == 2
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SpecificationArtifactRevision)
                .where(SpecificationArtifactRevision.interview_id == interview_id)
            )
            == 2
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SpecificationAnswer)
                .where(SpecificationAnswer.interview_id == interview_id)
            )
            == 1
        )

        with pytest.raises(HTTPException) as unauthorized_model:
            await generate_architect_draft(
                db,
                interview_id=interview.id,
                actor=actor,
                expected_revision=revised.revision,
                authorize_model_generation=False,
                idempotency_key="no-model-call",
            )
        assert unauthorized_model.value.status_code == 403


@pytest.mark.anyio
async def test_changed_default_branch_invalidates_readiness_before_scheduler_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, repository, _ = await _specification_fixture(
        tmp_path, monkeypatch
    )
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    source = Path(
        settings.openspec_repository_sources_json
        and json.loads(settings.openspec_repository_sources_json)[repository.target_id]
    )
    (source / "openspec/specs/access/new.md").write_text(
        "# New baseline\n\nThe main branch changed.\n", encoding="utf-8"
    )
    await asyncio.to_thread(_git, source, "add", "--all")
    await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "advance default branch",
        ],
        check=True,
    )

    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        interview, evaluation = await get_readiness(db, interview_id=interview_id, actor=actor)
        assert interview.readiness_state == "STALE"
        assert evaluation is not None and evaluation.state == "STALE"
        task = await db.get(Task, interview.task_id, populate_existing=True)
        assert task is not None and task.stage == TaskStage.SPECIFYING.value
        scheduler = Scheduler(global_limit=1)
        assert await scheduler.claim("specification-test-worker", "fixture") is None
        assert (
            await db.scalar(select(func.count()).select_from(Run).where(Run.task_id == task.id))
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SpecificationReadinessEvaluation)
                .where(
                    SpecificationReadinessEvaluation.interview_id == interview_id,
                    SpecificationReadinessEvaluation.state == "STALE",
                )
            )
            == 1
        )


@pytest.mark.anyio
async def test_agent_clarification_is_durable_fenced_idempotent_and_scope_pausing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    scheduler = Scheduler(global_limit=1, lease_seconds=30)
    job = await scheduler.claim("specification-agent-fixture", "fixture")
    assert job is not None

    async with SessionLocal() as db:
        question = await create_agent_question(
            db,
            interview_id=interview_id,
            task_id=job.task_id,
            run_id=job.run_id,
            attempt=job.attempt,
            fencing_token=job.fencing_token,
            question_text="Which authorization roles can approve this task?",
        )
        repeated = await create_agent_question(
            db,
            interview_id=interview_id,
            task_id=job.task_id,
            run_id=job.run_id,
            attempt=job.attempt,
            fencing_token=job.fencing_token,
            question_text="Which authorization roles can approve this task?",
        )
        assert repeated.id == question.id
        assert question.classification == "blocking"
        assert question.status == "open"
        assert question.evidence

    waiter = asyncio.create_task(
        wait_for_agent_answer(
            question_id=question.id,
            task_id=job.task_id,
            run_id=job.run_id,
            fencing_token=job.fencing_token,
        )
    )
    await asyncio.sleep(0.05)
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        _answer, changed = await answer_question(
            db,
            interview_id=interview_id,
            question_id=question.id,
            actor=actor,
            expected_revision=interview.revision,
            answer="The project administrator role may approve it.",
            idempotency_key="agent-question-answer-one",
            changes_scope=False,
        )
        assert changed.revision == 2
        task = await db.get(Task, job.task_id, populate_existing=True)
        assert task is not None
        assert task.stage == TaskStage.IN_PROGRESS.value
        assert task.run_status == RunStatus.RUNNING.value
    assert await asyncio.wait_for(waiter, timeout=2) == (
        "The project administrator role may approve it.",
        False,
    )

    async with SessionLocal() as db:
        second_question = await create_agent_question(
            db,
            interview_id=interview_id,
            task_id=job.task_id,
            run_id=job.run_id,
            attempt=job.attempt,
            fencing_token=job.fencing_token,
            question_text="Should this add a new repository capability?",
        )
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        await answer_question(
            db,
            interview_id=interview_id,
            question_id=second_question.id,
            actor=actor,
            expected_revision=interview.revision,
            answer="Yes, add a separate repository capability.",
            idempotency_key="agent-question-answer-scope",
            changes_scope=True,
        )
        task = await db.get(Task, job.task_id, populate_existing=True)
        run = await db.get(Run, job.run_id, populate_existing=True)
        assert task is not None and task.stage == TaskStage.SPECIFYING.value
        assert task.run_status == RunStatus.PAUSED.value
        assert run is not None and run.cancel_requested_at is not None
        assert (
            await db.scalar(select(func.count()).select_from(Run).where(Run.task_id == job.task_id))
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(
                    DomainEvent.run_id == job.run_id,
                    DomainEvent.event_type == "task.clarification_resumed",
                )
            )
            == 1
        )


@pytest.mark.anyio
async def test_answer_after_clarification_lease_loss_rechecks_and_seeds_recovery_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    interview_id = await _create_ready_interview(project_id, repository_id, actor)
    scheduler = Scheduler(global_limit=1, lease_seconds=30)
    job = await scheduler.claim("specification-recovery-fixture", "fixture")
    assert job is not None
    checkpoint = {
        "kind": "acp_execution",
        "resumable": True,
        "task_id": str(job.task_id),
        "run_id": str(job.run_id),
        "attempt": job.attempt,
        "fencing_token": job.fencing_token,
        "base_commit": "",
        "phase": "prompt_running",
    }
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        checkpoint["base_commit"] = interview.base_sha
    await scheduler.save_checkpoint(job, checkpoint)

    async with SessionLocal() as db:
        question = await create_agent_question(
            db,
            interview_id=interview_id,
            task_id=job.task_id,
            run_id=job.run_id,
            attempt=job.attempt,
            fencing_token=job.fencing_token,
            question_text="Which role may approve this repository workflow?",
        )
        lease = await db.scalar(select(TaskLease).where(TaskLease.task_id == job.task_id))
        assert lease is not None
        lease.expires_at = func.now() - timedelta(seconds=1)
        await db.commit()

    assert await scheduler.recover_expired_leases() == 1
    async with SessionLocal() as db:
        expired_run = await db.get(Run, job.run_id)
        task = await db.get(Task, job.task_id, populate_existing=True)
        assert expired_run is not None and expired_run.status == RunStatus.FAILED.value
        assert task is not None and task.stage == TaskStage.CLARIFYING.value
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        answer_text = "Only a project administrator may approve this workflow."
        answer, updated = await answer_question(
            db,
            interview_id=interview_id,
            question_id=question.id,
            actor=actor,
            expected_revision=interview.revision,
            answer=answer_text,
            idempotency_key="clarification-after-expiry",
            changes_scope=False,
        )
        assert answer.answer == answer_text
        assert updated.readiness_state == "READY"
        evaluation = await db.scalar(
            select(SpecificationReadinessEvaluation)
            .where(SpecificationReadinessEvaluation.interview_id == interview_id)
            .order_by(SpecificationReadinessEvaluation.created_at.desc())
            .limit(1)
        )
        assert evaluation is not None and evaluation.state == "READY"
        assert evaluation.interview_revision == updated.revision
        assert evaluation.artifact_revision == updated.artifact_revision
        assert evaluation.content_hash == updated.artifact_hash
        assert evaluation.base_sha == updated.base_sha
        task = await db.get(Task, job.task_id, populate_existing=True)
        assert task is not None and task.stage == TaskStage.READY.value
        assert task.blocked_reason == "worker_lost_checkpoint"
        recovery_event = await db.scalar(
            select(DomainEvent).where(
                DomainEvent.run_id == job.run_id,
                DomainEvent.event_type == "task.clarification_recovery_queued",
            )
        )
        assert recovery_event is not None
        assert recovery_event.payload["artifact_revision"] == updated.artifact_revision
        assert recovery_event.payload["readiness_evaluation_id"] == str(evaluation.id)

    recovered = await scheduler.claim("specification-recovery-fixture", "fixture")
    assert recovered is not None
    assert recovered.attempt == 2
    assert recovered.checkpoint is not None
    assert recovered.checkpoint["run_id"] == str(job.run_id)

    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        context = await _latest_specification_context(
            manager,
            interview,
            repository,
            expected_base_commit=recovered.checkpoint["base_commit"],
        )
    prompt = _task_prompt(
        recovered.task,
        context["base_sha"],
        {
            "run_id": recovered.checkpoint["run_id"],
            "base_commit": recovered.checkpoint["base_commit"],
            "phase": recovered.checkpoint["phase"],
            "attempt": recovered.checkpoint["attempt"],
        },
        specification_context=context,
    )
    task_path = f"openspec/changes/{interview.change_id}/tasks.md"
    assert context["artifact_revision"] == updated.artifact_revision
    assert context["head_sha"] == updated.head_sha
    assert answer_text in context["files"][task_path]
    assert "## Verification" in context["files"][task_path]
    assert answer_text in prompt
    assert "Use them as the current task specification" in prompt
    assert "lease/fencing" in prompt


@pytest.mark.anyio
async def test_authenticated_specification_api_and_fake_architect_are_revision_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)

    class FixtureArchitect:
        async def generate(self, _db, **context):
            change_id = context["change_id"]
            files = _initial_bundle(context["topic"], change_id)
            proposal_path = f"openspec/changes/{change_id}/proposal.md"
            files[proposal_path] += "\n## Fixture clarification\n\nConfirm the operator boundary.\n"
            return ArchitectDraft.model_validate(
                {
                    "proposal_md": files[proposal_path],
                    "spec_md": files[f"openspec/changes/{change_id}/specs/{change_id}/spec.md"],
                    "design_md": files[f"openspec/changes/{change_id}/design.md"],
                    "tasks_md": files[f"openspec/changes/{change_id}/tasks.md"],
                    "questions": [
                        {
                            "question": "Which operator role may approve this workflow?",
                            "classification": "blocking",
                            "proposed_assumption": "Only an authorized project reviewer may approve.",
                            "recommended_options": [
                                {
                                    "label": "Reviewer",
                                    "description": "Require an explicit reviewer membership.",
                                }
                            ],
                            "affected_artifacts": [proposal_path],
                        }
                    ],
                }
            )

    monkeypatch.setattr(specification_service, "_architect", FixtureArchitect())
    origin = settings.allowed_origins.split(",")[0].strip()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            login = await client.post(
                "/auth/cookie/login",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                data={"username": actor.email, "password": "fixture-password-long-enough"},
            )
            assert login.status_code == 204
            write_headers = {
                "Origin": origin,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "api-specification-topic",
            }
            path = f"/projects/{project_id}/specification-interviews"
            payload = {
                "repository_id": str(repository_id),
                "topic": "Add a scoped task acceptance workflow",
            }
            started = await client.post(path, headers=write_headers, json=payload)
            assert started.status_code == 201, started.text
            interview = started.json()
            interview_id = interview["id"]
            replay = await client.post(path, headers=write_headers, json=payload)
            assert replay.status_code == 200
            assert replay.headers["Idempotency-Replayed"] == "true"

            scoped_path = f"{path}/{interview_id}"
            assert (await client.get(scoped_path)).status_code == 200
            questions = await client.get(f"{scoped_path}/questions")
            assert questions.status_code == 200 and questions.json()
            artifacts = await client.get(f"{scoped_path}/artifacts")
            assert artifacts.status_code == 200
            assert artifacts.headers["ETag"]
            assert len(artifacts.json()["paths"]) == 4

            search = await client.post(
                f"/projects/{project_id}/repositories/{repository_id}/specification-search",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                json={"query": "permissions", "limit": 4},
            )
            assert search.status_code == 200
            assert any(item["untrusted_source_content"] for item in search.json()["results"])
            explorer = await client.get(
                f"/projects/{project_id}/repositories/{repository_id}/specification-explorer?limit=1"
            )
            assert explorer.status_code == 200 and len(explorer.json()["entries"]) == 1

            unauthorized_architect = await client.post(
                f"{scoped_path}/architect",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "api-architect-disabled",
                },
                json={
                    "expected_revision": interview["revision"],
                    "authorize_model_generation": False,
                },
            )
            assert unauthorized_architect.status_code == 403
            architect = await client.post(
                f"{scoped_path}/architect",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "api-architect-fixture",
                },
                json={
                    "expected_revision": interview["revision"],
                    "authorize_model_generation": True,
                },
            )
            assert architect.status_code == 200, architect.text
            assert len(architect.json()["questions"]) == 1
            architect_replay = await client.post(
                f"{scoped_path}/architect",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "api-architect-fixture",
                },
                json={
                    "expected_revision": interview["revision"],
                    "authorize_model_generation": True,
                },
            )
            assert architect_replay.status_code == 200

            questions = await client.get(f"{scoped_path}/questions")
            question_id = next(
                item["id"] for item in questions.json() if item["source"] == "architect"
            )
            answer = await client.post(
                f"{scoped_path}/questions/{question_id}/answers",
                headers={"Origin": origin, "X-CSRF-Token": csrf, "Idempotency-Key": "api-answer"},
                json={
                    "expected_revision": architect.json()["revision"],
                    "answer": "A project reviewer may approve the workflow.",
                    "changes_scope": False,
                },
            )
            assert answer.status_code == 200, answer.text
            assert answer.headers["ETag"]

            edited_artifacts = artifacts.json()
            edit_body = {
                "expected_revision": answer.json()["interview"]["revision"],
                "proposal_md": edited_artifacts["proposal_md"] + "\nAdditional API edit.\n",
                "spec_md": edited_artifacts["spec_md"],
                "design_md": edited_artifacts["design_md"],
                "tasks_md": edited_artifacts["tasks_md"],
            }
            stale_edit = await client.put(
                f"{scoped_path}/artifacts",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "If-Match": 'W/"999"',
                },
                json=edit_body,
            )
            assert stale_edit.status_code == 412
            edited = await client.put(
                f"{scoped_path}/artifacts",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "If-Match": f'W/"{edit_body["expected_revision"]}"',
                },
                json=edit_body,
            )
            assert edited.status_code == 200, edited.text

            readiness = await client.get(f"{scoped_path}/readiness")
            assert readiness.status_code == 200
            evaluated = await client.post(
                f"{scoped_path}/readiness/evaluate",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
            )
            assert evaluated.status_code == 200
            signoff = await client.post(
                f"{scoped_path}/readiness/sign-off",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                json={
                    "expected_revision": edited.json()["revision"],
                    "expected_artifact_revision": edited.json()["artifact_revision"],
                },
            )
            assert signoff.status_code == 200, signoff.text


@pytest.mark.anyio
async def test_invalid_architect_openspec_bundle_is_persisted_but_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, _, _ = await _specification_fixture(tmp_path, monkeypatch)

    class InvalidFixtureArchitect:
        async def generate(self, _db, **context):
            change_id = context["change_id"]
            files = _initial_bundle(context["topic"], change_id)
            spec_path = f"openspec/changes/{change_id}/specs/{change_id}/spec.md"
            files[spec_path] = "# Incomplete specification\n"
            return ArchitectDraft.model_validate(
                {
                    "proposal_md": files[f"openspec/changes/{change_id}/proposal.md"],
                    "spec_md": files[spec_path],
                    "design_md": files[f"openspec/changes/{change_id}/design.md"],
                    "tasks_md": files[f"openspec/changes/{change_id}/tasks.md"],
                    "questions": [
                        {
                            "question": "Which verification should be required?",
                            "classification": "nonblocking",
                            "proposed_assumption": "Use a focused unit test.",
                            "recommended_options": [],
                            "affected_artifacts": [spec_path],
                        }
                    ],
                }
            )

    monkeypatch.setattr(specification_service, "_architect", InvalidFixtureArchitect())
    origin = settings.allowed_origins.split(",")[0].strip()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            login = await client.post(
                "/auth/cookie/login",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                data={"username": actor.email, "password": "fixture-password-long-enough"},
            )
            assert login.status_code == 204
            path = f"/projects/{project_id}/specification-interviews"
            started = await client.post(
                path,
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "api-invalid-architect-topic",
                },
                json={
                    "repository_id": str(repository_id),
                    "topic": "Add an Architect validation recovery scenario",
                },
            )
            assert started.status_code == 201, started.text
            interview = started.json()
            architect = await client.post(
                f"{path}/{interview['id']}/architect",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "api-invalid-architect-result",
                },
                json={
                    "expected_revision": interview["revision"],
                    "authorize_model_generation": True,
                },
            )
            assert architect.status_code == 200, architect.text
            assert architect.json()["artifact_revision"] == 2
            assert len(architect.json()["questions"]) == 1

            persisted = await client.get(f"{path}/{interview['id']}")
            assert persisted.status_code == 200
            assert persisted.json()["readiness_state"] == "NOT_READY"
            assert persisted.json()["artifact_revision"] == 2
