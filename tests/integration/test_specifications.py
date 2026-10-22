from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from harnessa.adapters.specification_architect import ArchitectDraft
from harnessa.adapters.specification_workspace import SpecificationWorkspaceManager
from harnessa.config import settings
from harnessa.contracts import RunStatus, TaskStage
from harnessa.db.models import (
    Membership,
    Organization,
    Project,
    ProjectMembership,
    Repository,
    Task,
    User,
)
from harnessa.db.orchestration import DomainEvent, Run
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
from sqlalchemy import func, select

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
    await asyncio.to_thread(subprocess.run, ["git", "init", "--quiet", "--initial-branch=main", str(source)], check=True)
    (source / "openspec/specs/access").mkdir(parents=True)
    (source / "openspec/specs/access/spec.md").write_text(
        "# Access\n\nCheck permissions before creating tasks.\n", encoding="utf-8"
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
) -> UUID:
    async with SessionLocal() as db:
        interview, replayed = await create_interview(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            topic="Add a scoped task acceptance workflow",
            idempotency_key=idempotency_key,
        )
        assert replayed is False
        interview, evaluation = await evaluate_readiness(
            db, interview_id=interview.id, actor=actor
        )
        assert evaluation.state == "READY"
        assert evaluation.score == 1
        return interview.id


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

        revised, evaluation = await evaluate_readiness(
            db, interview_id=interview.id, actor=actor
        )
        assert evaluation.state == "READY"
        assert revised.artifact_revision == 2
        assert await db.scalar(
            select(func.count()).select_from(SpecificationArtifactRevision).where(
                SpecificationArtifactRevision.interview_id == interview_id
            )
        ) == 2
        assert await db.scalar(
            select(func.count()).select_from(SpecificationAnswer).where(
                SpecificationAnswer.interview_id == interview_id
            )
        ) == 1

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
    source = Path(settings.openspec_repository_sources_json and json.loads(
        settings.openspec_repository_sources_json
    )[repository.target_id])
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
        assert await db.scalar(
            select(func.count()).select_from(Run).where(Run.task_id == task.id)
        ) == 0
        assert await db.scalar(
            select(func.count()).select_from(SpecificationReadinessEvaluation).where(
                SpecificationReadinessEvaluation.interview_id == interview_id,
                SpecificationReadinessEvaluation.state == "STALE",
            )
        ) == 1


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
        assert await db.scalar(
            select(func.count()).select_from(Run).where(Run.task_id == job.task_id)
        ) == 1
        assert await db.scalar(
            select(func.count()).select_from(DomainEvent).where(
                DomainEvent.run_id == job.run_id,
                DomainEvent.event_type == "task.clarification_resumed",
            )
        ) == 1


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
                    "spec_md": files[
                        f"openspec/changes/{change_id}/specs/{change_id}/spec.md"
                    ],
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
