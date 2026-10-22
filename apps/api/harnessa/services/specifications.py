"""Durable OpenSpec interviews, answers, revisions, and readiness gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from harnessa.adapters.specification_architect import (
    SpecificationArchitect,
    SpecificationArchitectError,
)
from harnessa.adapters.specification_workspace import (
    OpenSpecValidation,
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.config import settings
from harnessa.contracts import ModelPoolKind, ModelProtocol, RouteRequirements, RunStatus, TaskStage
from harnessa.db.model_gateway import ModelBudgetPolicy
from harnessa.db.models import Membership, Project, ProjectMembership, Repository, Task, User
from harnessa.db.orchestration import Run, TaskLease, TaskTransition
from harnessa.db.session import SessionLocal
from harnessa.db.specification import (
    SpecificationAnswer,
    SpecificationArtifactRevision,
    SpecificationInterview,
    SpecificationQuestion,
    SpecificationReadinessEvaluation,
)
from harnessa.domain.lifecycle import assert_transition
from harnessa.domain.retrieval import (
    RepositoryCitation,
    RepositorySearchResult,
    RetrievalContextManifest,
    RetrievalManifestItem,
)
from harnessa.services.authorization import require_membership
from harnessa.services.lifecycle import (
    create_task,
    record_domain_event,
    record_system_transition,
    transition_task,
)
from harnessa.services.model_gateway import has_compatible_configured_route
from harnessa.services.retrieval_index import approved_skill_paths_for_project
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

_workspace_manager = SpecificationWorkspaceManager()
_architect = SpecificationArchitect()
_MAX_DIFF_BYTES = 100_000
_CHANGE_SLUG_WORDS = 6
_POLICY_REVISION = "m6-readiness-v3"
_ARTIFACT_NAMES = {
    "proposal_md": "proposal.md",
    "spec_md": "spec.md",
    "design_md": "design.md",
    "tasks_md": "tasks.md",
}


def _request_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _check_operator(role: str) -> None:
    if role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required")


def _slug(topic: str) -> str:
    ascii_topic = unicodedata.normalize("NFKD", topic).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_topic.casefold())[:_CHANGE_SLUG_WORDS]
    return "-".join(words)[:60].strip("-") or "specification-change"


def _plain_topic(topic: str) -> str:
    return " ".join(topic.split()).replace("\x00", "")


def _initial_bundle(topic: str, change_id: str) -> dict[str, str]:
    topic_text = _plain_topic(topic).replace("`", "'")
    capability = " ".join(part.capitalize() for part in change_id.split("-"))
    spec_path = f"openspec/changes/{change_id}/specs/{change_id}/spec.md"
    return {
        f"openspec/changes/{change_id}/proposal.md": (
            "## Why\n\n"
            f"The requested change addresses: `{topic_text}`.\n\n"
            "## What Changes\n\n"
            "- Add the requested behavior within the existing repository boundaries.\n"
            "- Preserve current authorization, lifecycle, and persistence contracts.\n\n"
            "## Capabilities\n\n"
            f"### {change_id}\n\n"
            f"Specify and verify the requested behavior described by `{topic_text}`.\n\n"
            "## Impact\n\n"
            "Repository implementation and its focused automated verification.\n"
        ),
        spec_path: (
            "## ADDED Requirements\n\n"
            f"### Requirement: {capability}\n\n"
            f"The system SHALL address the requested behavior: `{topic_text}`.\n\n"
            "#### Scenario: Requested behavior is available\n\n"
            "- **WHEN** an authorized user requests the behavior\n"
            "- **THEN** the system provides the behavior described by this requirement\n"
        ),
        f"openspec/changes/{change_id}/design.md": (
            "## Context\n\n"
            f"The repository topic is `{topic_text}`. This draft follows existing architecture "
            "and leaves implementation details open to the repository's current patterns.\n\n"
            "## Goals and Non-Goals\n\n"
            "- Goal: implement and verify the requested behavior.\n"
            "- Non-goal: change unrelated product or runtime boundaries.\n\n"
            "## Decisions\n\n"
            "- Preserve current application ownership and reuse the existing supported stack.\n\n"
            "## Risks / Trade-offs\n\n"
            "- The exact edge cases need confirmation through repository evidence and tests.\n"
        ),
        f"openspec/changes/{change_id}/tasks.md": (
            "## 1. Implementation\n\n"
            f"- [ ] Implement the `{capability}` requirement within existing module boundaries.\n\n"
            "## Verification\n\n"
            f"- [ ] Verify requirement `{capability}` with focused automated tests.\n"
            "- [ ] Run the applicable project lint, type, and test gates.\n\n"
            f"### Requirement mapping: {capability}\n\n"
            "Verify the requested behavior and its authorization boundary with focused tests.\n"
        ),
    }


def _citations_payload(search_result: Any) -> list[dict[str, Any]]:
    return [
        {
            "revision": item.revision,
            "path": item.path,
            "line": item.line,
            "snippet": item.snippet,
            "matched_terms": list(item.matched_terms),
            "untrusted_source_content": True,
        }
        for item in search_result.citations
    ]


async def _interview_for_update(
    db: AsyncSession,
    interview_id: UUID,
    actor: User,
    *,
    operator_only: bool = True,
    lock: bool = True,
) -> tuple[SpecificationInterview, Repository, Task, Membership | ProjectMembership]:
    query = select(SpecificationInterview).where(SpecificationInterview.id == interview_id)
    if lock:
        query = query.with_for_update()
    interview = await db.scalar(query.execution_options(populate_existing=True))
    if interview is None:
        raise HTTPException(404, "Specification interview not found")
    membership = await require_membership(db, actor, interview.project_id)
    if operator_only:
        _check_operator(membership.role)
    repository = await db.get(Repository, interview.repository_id)
    task = await db.get(Task, interview.task_id)
    if repository is None or task is None or repository.project_id != interview.project_id:
        raise HTTPException(409, "Specification interview scope is no longer valid")
    return interview, repository, task, membership


async def _emit(
    db: AsyncSession,
    task: Task,
    actor_id: UUID | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: UUID | None = None,
) -> None:
    await record_domain_event(
        db,
        task=task,
        event_type=event_type,
        actor_id=actor_id,
        payload=payload,
        run_id=run_id,
    )


async def _set_task_state(
    db: AsyncSession,
    *,
    task: Task,
    actor_id: UUID | None,
    target: TaskStage,
    run_status: str,
    reason: str,
    command: str,
    event_type: str,
    return_stage: TaskStage | None = None,
    run_id: UUID | None = None,
    recovery: bool = False,
) -> None:
    current = TaskStage(task.stage)
    assert_transition(
        current,
        target,
        expected_version=task.version,
        current_version=task.version,
        reason=reason,
        return_stage=return_stage or (TaskStage(task.return_stage) if task.return_stage else None),
        recovery=recovery,
    )
    version_before = task.version
    old_status = task.run_status
    task.stage = target.value
    task.run_status = run_status
    task.return_stage = return_stage.value if return_stage else None
    task.version += 1
    db.add(
        TaskTransition(
            task_id=task.id,
            project_id=task.project_id,
            actor_id=actor_id,
            from_stage=current.value,
            to_stage=target.value,
            from_run_status=old_status,
            to_run_status=run_status,
            reason=reason,
            command=command,
            version_before=version_before,
            version_after=task.version,
        )
    )
    await _emit(
        db,
        task,
        actor_id,
        event_type,
        {
            "from_stage": current.value,
            "to_stage": target.value,
            "from_run_status": old_status,
            "run_status": run_status,
            "reason": reason,
            "version": task.version,
        },
        run_id=run_id,
    )


async def create_agent_question(
    db: AsyncSession,
    *,
    interview_id: UUID,
    task_id: UUID,
    run_id: UUID,
    attempt: int,
    fencing_token: int,
    question_text: str,
) -> SpecificationQuestion:
    if not 12 <= len(question_text.strip()) <= 2000:
        raise HTTPException(422, "Clarification question must contain between 12 and 2000 characters")
    interview = await db.scalar(
        select(SpecificationInterview)
        .where(
            SpecificationInterview.id == interview_id,
            SpecificationInterview.task_id == task_id,
        )
        .with_for_update()
    )
    if interview is None:
        raise HTTPException(404, "Run has no specification interview")
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    lease = await db.scalar(
        select(TaskLease)
        .where(
            TaskLease.task_id == task_id,
            TaskLease.run_id == run_id,
            TaskLease.fencing_token == fencing_token,
            TaskLease.owner_id.is_not(None),
            TaskLease.expires_at > func.now(),
        )
        .with_for_update()
    )
    if (
        task is None
        or run is None
        or lease is None
        or run.task_id != task.id
        or run.project_id != interview.project_id
        or run.attempt != attempt
        or run.fencing_token != fencing_token
        or run.status != "RUNNING"
        or run.cancel_requested_at is not None
        or task.cancel_requested_at is not None
    ):
        raise HTTPException(409, "Clarification request has a stale run or fencing token")
    question = await db.scalar(
        select(SpecificationQuestion)
        .where(
            SpecificationQuestion.interview_id == interview.id,
            SpecificationQuestion.source == "agent",
            SpecificationQuestion.source_revision == interview.revision,
            SpecificationQuestion.status == "open",
            SpecificationQuestion.question == question_text.strip(),
        )
        .with_for_update()
    )
    if (
        task.stage == TaskStage.CLARIFYING.value
        and task.run_status == RunStatus.WAITING_FOR_INPUT.value
        and question is not None
        and question.run_id == run.id
        and question.attempt == attempt
        and question.fencing_token == fencing_token
    ):
        return question
    if task.stage != TaskStage.IN_PROGRESS.value or task.run_status != RunStatus.RUNNING.value:
        raise HTTPException(409, "Clarification request has a stale task state")
    open_question = await db.scalar(
        select(SpecificationQuestion.id)
        .where(
            SpecificationQuestion.interview_id == interview.id,
            SpecificationQuestion.source == "agent",
            SpecificationQuestion.run_id == run.id,
            SpecificationQuestion.status == "open",
        )
        .limit(1)
    )
    if open_question is not None and (question is None or open_question != question.id):
        raise HTTPException(409, "This run already has a different open clarification question")
    if question is None:
        repository = await db.get(Repository, interview.repository_id)
        if repository is None:
            raise HTTPException(409, "Specification repository is no longer connected")
        try:
            evidence_result = await _workspace_manager.search(
                repository.target_id, repository.default_branch, question_text, limit=6
            )
        except SpecificationWorkspaceError as exc:
            raise HTTPException(503, str(exc)) from exc
        question = SpecificationQuestion(
            interview_id=interview.id,
            task_id=task.id,
            run_id=run.id,
            source="agent",
            source_revision=interview.revision,
            attempt=attempt,
            fencing_token=fencing_token,
            classification="blocking",
            status="open",
            question=question_text.strip(),
            proposed_assumption=None,
            recommended_options=[],
            affected_artifacts=list(_workspace_manager._bundle_paths(interview.change_id)),
            evidence=_citations_payload(evidence_result),
        )
        db.add(question)
        await db.flush()
        await _emit(
            db,
            task,
            None,
            "specification.agent_question_asked",
            {
                "interview_id": str(interview.id),
                "question_id": str(question.id),
                "source_revision": interview.revision,
                "run_id": str(run.id),
                "attempt": attempt,
                "fencing_token": fencing_token,
            },
            run_id=run.id,
        )
    else:
        question.run_id = run.id
        question.attempt = attempt
        question.fencing_token = fencing_token
    if task.stage != TaskStage.CLARIFYING.value:
        await _set_task_state(
            db,
            task=task,
            actor_id=None,
            target=TaskStage.CLARIFYING,
            run_status="WAITING_FOR_INPUT",
            reason="Coding agent persisted a scoped clarification question",
            command="agent_clarification",
            event_type="task.waiting_for_clarification",
            return_stage=TaskStage.IN_PROGRESS,
            run_id=run.id,
        )
    await db.commit()
    await db.refresh(question)
    return question


async def wait_for_agent_answer(
    *,
    question_id: UUID,
    task_id: UUID,
    run_id: UUID,
    fencing_token: int,
    sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
) -> tuple[str, bool]:
    while True:
        async with sessions() as db:
            question = await db.get(SpecificationQuestion, question_id)
            task = await db.get(Task, task_id)
            run = await db.get(Run, run_id)
            lease = await db.scalar(
                select(TaskLease).where(
                    TaskLease.task_id == task_id,
                    TaskLease.run_id == run_id,
                    TaskLease.fencing_token == fencing_token,
                    TaskLease.owner_id.is_not(None),
                    TaskLease.expires_at > func.now(),
                )
            )
            if question is None or task is None or run is None:
                raise HTTPException(409, "Clarification scope disappeared while waiting")
            if question.status == "answered":
                answer = await db.scalar(
                    select(SpecificationAnswer)
                    .where(SpecificationAnswer.question_id == question_id)
                    .order_by(SpecificationAnswer.answer_revision.desc())
                    .limit(1)
                )
                if answer is None:
                    raise HTTPException(409, "Answer receipt is missing")
                return answer.answer, bool(answer.provenance.get("scope_change"))
            if (
                question.status != "open"
                or lease is None
                or run.status != "RUNNING"
                or run.cancel_requested_at is not None
                or task.cancel_requested_at is not None
                or task.run_status in {RunStatus.CANCELLED.value, RunStatus.PAUSED.value}
            ):
                raise HTTPException(409, "Clarification run is no longer active")
            if run.deadline_at is not None and run.deadline_at <= datetime.now(UTC):
                raise HTTPException(408, "Clarification wait reached the run deadline")
        await asyncio.sleep(0.5)


async def _save_artifact_revision(
    db: AsyncSession,
    *,
    interview: SpecificationInterview,
    task: Task,
    actor: User,
    repository: Repository,
    files: dict[str, str],
    summary: str,
) -> tuple[str, str, list[str]]:
    workspace = await _workspace_manager.open(
        interview.id,
        repository,
        change_id=interview.change_id,
        base_sha=interview.base_sha,
        head_sha=interview.head_sha,
        allow_one_pending_commit=True,
    )
    try:
        head, digest, paths = await _workspace_manager.commit_bundle(
            workspace,
            files,
            revision=interview.artifact_revision + 1,
            expected_head=interview.head_sha,
        )
    except SpecificationWorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    revision = interview.artifact_revision + 1
    interview.revision += 1
    interview.artifact_revision = revision
    interview.head_sha = head
    interview.artifact_hash = digest
    interview.readiness_state = "STALE"
    interview.readiness_snapshot = None
    db.add(
        SpecificationArtifactRevision(
            interview_id=interview.id,
            revision=revision,
            base_sha=interview.base_sha,
            commit_sha=head,
            content_hash=digest,
            paths=paths,
            change_summary=summary[:2000],
            created_by=actor.id,
        )
    )
    await db.execute(
        text(
            "UPDATE specification_questions SET status = 'stale', updated_at = now() "
            "WHERE interview_id = :interview_id AND status = 'open'"
        ),
        {"interview_id": interview.id},
    )
    await _emit(
        db,
        task,
        actor.id,
        "specification.artifacts_revised",
        {
            "interview_id": str(interview.id),
            "artifact_revision": revision,
            "commit_sha": head,
            "content_hash": digest,
            "base_sha": interview.base_sha,
            "policy_revision": interview.policy_revision,
        },
    )
    return head, digest, paths


async def create_interview(
    db: AsyncSession,
    *,
    project_id: UUID,
    repository_id: UUID,
    actor: User,
    topic: str,
    idempotency_key: str,
) -> tuple[SpecificationInterview, bool]:
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    membership = await require_membership(db, actor, project_id)
    _check_operator(membership.role)
    request = {"repository_id": str(repository_id), "topic": topic.strip()}
    request_digest = _request_hash(request)
    project = await db.get(Project, project_id)
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
        )
    )
    if project is None or repository is None:
        raise HTTPException(404, "Repository is not connected to this project")
    try:
        await _workspace_manager.source_path(repository)
    except (OSError, SpecificationWorkspaceError) as exc:
        raise HTTPException(409, str(exc)) from exc

    lock_key = f"specification-interview:{project_id}:{idempotency_key}"
    await db.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": lock_key})
    try:
        existing = await db.scalar(
            select(SpecificationInterview).where(
                SpecificationInterview.project_id == project_id,
                SpecificationInterview.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_hash != request_digest:
                raise HTTPException(409, "Idempotency-Key was already used with a different interview")
            return existing, True

        current_base = await _workspace_manager.current_base(repository)
        command_key = f"openspec-task:{_request_hash({'project': str(project_id), 'key': idempotency_key})}"
        task, task_replayed = await create_task(
            db,
            project_id=project_id,
            actor=actor,
            title=topic.strip()[:300],
            description=topic.strip(),
            priority=0,
            command_key=command_key,
        )
        if task.stage == TaskStage.BACKLOG.value:
            task, _ = await transition_task(
                db,
                task_id=task.id,
                actor=actor,
                target=TaskStage.SPECIFYING,
                expected_version=task.version,
                reason="OpenSpec topic interview started",
                return_stage=None,
                command_key=f"{command_key}:specifying",
            )
        elif task.stage not in {TaskStage.SPECIFYING.value, TaskStage.READY.value}:
            raise HTTPException(409, "Idempotent task is no longer in specification intake")

        interview_id = uuid5(task.id, f"harnessa-openspec:{idempotency_key}")
        change_id = f"{_slug(topic)}-{interview_id.hex[:8]}"
        files = _initial_bundle(topic, change_id)
        try:
            search = await _workspace_manager.search(
                repository.target_id, repository.default_branch, topic, limit=8
            )
            workspace, head, digest = await _workspace_manager.create(
                interview_id, repository, change_id, files
            )
            if workspace.base_sha != current_base:
                raise HTTPException(409, "Repository base changed while the interview workspace was created")
            validation = await _workspace_manager.validate(workspace)
        except SpecificationWorkspaceError as exc:
            raise HTTPException(503, str(exc)) from exc

        row = SpecificationInterview(
            id=interview_id,
            organization_id=project.organization_id,
            project_id=project.id,
            task_id=task.id,
            repository_id=repository.id,
            created_by=actor.id,
            topic=topic.strip(),
            change_id=change_id,
            base_sha=current_base,
            head_sha=head,
            idempotency_key=idempotency_key,
            request_hash=request_digest,
            revision=1,
            artifact_revision=1,
            artifact_hash=digest,
            policy_revision=_POLICY_REVISION,
            readiness_state="NOT_EVALUATED",
            readiness_snapshot={"task_replayed": task_replayed},
        )
        db.add(row)
        await db.flush()
        db.add(
            SpecificationArtifactRevision(
                interview_id=interview_id,
                revision=1,
                base_sha=current_base,
                commit_sha=head,
                content_hash=digest,
                paths=list(files),
                change_summary="Initial OpenSpec topic draft",
                created_by=actor.id,
            )
        )
        citations = _citations_payload(search)
        db.add(
            SpecificationQuestion(
                interview_id=interview_id,
                task_id=task.id,
                source="interview",
                source_revision=1,
                classification="nonblocking",
                status="open",
                question="Are there repository-specific acceptance cases beyond the initial topic?",
                proposed_assumption=(
                    "Use the repository's existing conventions and add focused tests for the "
                    "behavior described by the topic."
                ),
                recommended_options=[
                    {"label": "Keep this assumption", "description": "Continue with the focused draft."},
                    {"label": "Add cases", "description": "Provide additional acceptance behavior."},
                ],
                affected_artifacts=list(files),
                evidence=citations,
            )
        )
        await _emit(
            db,
            task,
            actor.id,
            "specification.interview_created",
            {
                "interview_id": str(interview_id),
                "repository_id": str(repository.id),
                "base_sha": current_base,
                "head_sha": head,
                "artifact_revision": 1,
                "content_hash": digest,
                "policy_revision": _POLICY_REVISION,
                "openspec_version": validation.version,
            },
        )
        await db.commit()
        await db.refresh(row)
        return row, False
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": lock_key})


async def search_repository(
    db: AsyncSession,
    *,
    project_id: UUID,
    repository_id: UUID,
    actor: User,
    query: str,
    limit: int,
) -> RepositorySearchResult:
    await require_membership(db, actor, project_id)
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
        )
    )
    if repository is None:
        raise HTTPException(404, "Repository is not connected to this project")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    try:
        # Membership and repository ownership are re-read immediately before the
        # adapter opens source content. Neither an index nor a result cache grants access.
        await require_membership(db, actor, project_id)
        authorized_repository = await db.scalar(
            select(Repository).where(
                Repository.id == repository_id,
                Repository.project_id == project_id,
            )
        )
        if authorized_repository is None:
            raise HTTPException(404, "Repository is not connected to this project")
        result = await _workspace_manager.search(
            authorized_repository.target_id,
            authorized_repository.default_branch,
            query,
            limit,
            approved_skill_paths_for_project(project_id),
        )
    except (OSError, SpecificationWorkspaceError) as exc:
        raise HTTPException(503, str(exc)) from exc
    max_context_bytes = 24_000
    context_bytes = 0
    selected: list[RepositoryCitation] = []
    manifest_items: list[RetrievalManifestItem] = []
    omitted: list[str] = []
    for rank, citation in enumerate(result.citations, start=1):
        snippet_hash = hashlib.sha256(citation.snippet.encode("utf-8")).hexdigest()
        snippet_bytes = len(citation.snippet.encode("utf-8"))
        if context_bytes + snippet_bytes > max_context_bytes:
            omitted.append(f"{citation.path}:{citation.line}:context_budget")
            continue
        context_bytes += snippet_bytes
        source_id = hashlib.sha256(
            f"{project.organization_id}:{project_id}:{repository_id}:{citation.path}".encode()
        ).hexdigest()
        selected_citation = RepositoryCitation(
            revision=citation.revision,
            path=citation.path,
            line=citation.line,
            snippet=citation.snippet,
            matched_terms=citation.matched_terms,
            content_hash=snippet_hash,
        )
        selected.append(selected_citation)
        manifest_items.append(
            RetrievalManifestItem(
                source_id=source_id,
                source_uri=f"repo://{repository_id}/{citation.path}",
                revision=citation.revision,
                path=citation.path,
                line=citation.line,
                content_hash=snippet_hash,
                rank=rank,
                score=len(citation.matched_terms),
                stale=False,
            )
        )
    manifest = RetrievalContextManifest(
        schema_version=1,
        query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        policy_revision="repository-lexical-v1",
        organization_id=str(project.organization_id),
        project_id=str(project_id),
        repository_id=str(repository_id),
        sources_considered=(f"repo://{repository_id}@{result.revision}",),
        selected=tuple(manifest_items),
        omitted=tuple(omitted),
        max_context_bytes=max_context_bytes,
        context_bytes=context_bytes,
    )
    return RepositorySearchResult(result.revision, tuple(selected), manifest)


async def list_explorer_entries(
    db: AsyncSession,
    *,
    project_id: UUID,
    repository_id: UUID,
    actor: User,
) -> tuple[str, list[dict[str, Any]]]:
    await require_membership(db, actor, project_id)
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
        )
    )
    if repository is None:
        raise HTTPException(404, "Repository is not connected to this project")
    try:
        source = await _workspace_manager.source_path(repository)
        revision = await _workspace_manager.current_base(repository)
        tree = await asyncio.to_thread(
            _workspace_manager._git, source, "ls-tree", "-r", "--name-only", revision
        )
    except (OSError, SpecificationWorkspaceError) as exc:
        raise HTTPException(503, str(exc)) from exc
    entries = []
    for path in tree.splitlines():
        if not path.endswith(".md"):
            continue
        if path.startswith(("openspec/specs/", "openspec/changes/archive/")):
            entries.append(
                {
                    "revision": revision,
                    "path": path,
                    "section": Path(path).parent.name,
                    "archived": path.startswith("openspec/changes/archive/"),
                }
            )
    return revision, entries[:500]


async def read_artifacts(
    db: AsyncSession, *, interview_id: UUID, actor: User
) -> tuple[SpecificationInterview, dict[str, str], str]:
    interview, repository, _, _ = await _interview_for_update(
        db, interview_id, actor, operator_only=False, lock=False
    )
    try:
        workspace = await _workspace_manager.open(
            interview.id,
            repository,
            change_id=interview.change_id,
            base_sha=interview.base_sha,
            head_sha=interview.head_sha,
        )
        files = await _workspace_manager.read_bundle(workspace, interview.head_sha)
        diff = await _workspace_manager.artifact_diff(
            workspace, interview.base_sha, interview.head_sha
        )
    except SpecificationWorkspaceError as exc:
        raise HTTPException(503, str(exc)) from exc
    return interview, files, diff[:_MAX_DIFF_BYTES]


async def update_artifacts(
    db: AsyncSession,
    *,
    interview_id: UUID,
    actor: User,
    expected_revision: int,
    proposal_md: str,
    spec_md: str,
    design_md: str,
    tasks_md: str,
) -> SpecificationInterview:
    interview, repository, task, _ = await _interview_for_update(db, interview_id, actor)
    if interview.revision != expected_revision:
        raise HTTPException(409, "Specification interview revision changed")
    files = {
        f"openspec/changes/{interview.change_id}/{_ARTIFACT_NAMES['proposal_md']}": proposal_md,
        f"openspec/changes/{interview.change_id}/specs/{interview.change_id}/{_ARTIFACT_NAMES['spec_md']}": spec_md,
        f"openspec/changes/{interview.change_id}/{_ARTIFACT_NAMES['design_md']}": design_md,
        f"openspec/changes/{interview.change_id}/{_ARTIFACT_NAMES['tasks_md']}": tasks_md,
    }
    await _save_artifact_revision(
        db,
        interview=interview,
        task=task,
        actor=actor,
        repository=repository,
        files=files,
        summary="Operator edited the OpenSpec bundle",
    )
    if task.stage == TaskStage.READY.value:
        await transition_task(
            db,
            task_id=task.id,
            actor=actor,
            target=TaskStage.SPECIFYING,
            expected_version=task.version,
            reason="OpenSpec artifacts changed and readiness must be reassessed",
            return_stage=None,
            command_key=f"spec-edit:{interview.id}:{interview.artifact_revision}",
        )
    await db.commit()
    await db.refresh(interview)
    return interview


async def answer_question(
    db: AsyncSession,
    *,
    interview_id: UUID,
    question_id: UUID,
    actor: User,
    expected_revision: int,
    answer: str,
    idempotency_key: str,
    changes_scope: bool,
) -> tuple[SpecificationAnswer, SpecificationInterview]:
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    interview, repository, task, membership = await _interview_for_update(db, interview_id, actor)
    request_digest = _request_hash(
        {"question_id": str(question_id), "revision": expected_revision,
         "answer": answer, "changes_scope": changes_scope}
    )
    previous = await db.scalar(
        select(SpecificationAnswer).where(
            SpecificationAnswer.interview_id == interview_id,
            SpecificationAnswer.idempotency_key == idempotency_key,
        )
    )
    if previous is not None:
        if previous.request_hash != request_digest:
            raise HTTPException(409, "Idempotency-Key was already used with another answer")
        return previous, interview
    if interview.revision != expected_revision:
        raise HTTPException(409, "Answer references a stale specification revision")
    question = await db.scalar(
        select(SpecificationQuestion)
        .where(
            SpecificationQuestion.id == question_id,
            SpecificationQuestion.interview_id == interview_id,
        )
        .with_for_update()
    )
    if question is None:
        raise HTTPException(404, "Specification question not found")
    if question.status != "open" or question.source_revision != interview.revision:
        raise HTTPException(409, "Specification question is stale or already resolved")
    provenance = {
        "actor_id": str(actor.id),
        "project_role": membership.role,
        "scope_change": changes_scope,
        "source_revision": question.source_revision,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    answer_row = SpecificationAnswer(
        id=uuid4(),
        interview_id=interview.id,
        question_id=question.id,
        actor_id=actor.id,
        answer_revision=interview.revision + 1,
        idempotency_key=idempotency_key,
        request_hash=request_digest,
        answer=answer,
        provenance=provenance,
    )
    db.add(answer_row)
    question.status = "answered"
    workspace = await _workspace_manager.open(
        interview.id,
        repository,
        change_id=interview.change_id,
        base_sha=interview.base_sha,
        head_sha=interview.head_sha,
    )
    try:
        files = await _workspace_manager.read_bundle(workspace, interview.head_sha)
        task_path = f"openspec/changes/{interview.change_id}/tasks.md"
        safe_answer = "\n> ".join(answer.replace("\x00", "").splitlines())
        files[task_path] += (
            "\n## Clarifications\n\n"
            f"- Question {question.id} at revision {question.source_revision}:\n"
            f"  > {safe_answer}\n"
        )
        await _save_artifact_revision(
            db,
            interview=interview,
            task=task,
            actor=actor,
            repository=repository,
            files=files,
            summary=f"Answered clarification {question.id}",
        )
    except SpecificationWorkspaceError as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.flush()
    await _emit(
        db,
        task,
        actor.id,
        "specification.question_answered",
        {
            "interview_id": str(interview.id),
            "question_id": str(question.id),
            "answer_id": str(answer_row.id),
            "answer_revision": answer_row.answer_revision,
            "scope_change": changes_scope,
        },
        run_id=question.run_id,
    )
    active_run: Run | None = None
    if question.run_id is not None:
        active_run = await db.scalar(
            select(Run)
            .where(
                Run.id == question.run_id,
                Run.task_id == task.id,
                Run.attempt == question.attempt,
                Run.fencing_token == question.fencing_token,
                Run.status == "RUNNING",
            )
            .with_for_update()
        )
        active_lease = await db.scalar(
            select(TaskLease).where(
                TaskLease.task_id == task.id,
                TaskLease.run_id == question.run_id,
                TaskLease.fencing_token == question.fencing_token,
                TaskLease.owner_id.is_not(None),
                TaskLease.expires_at > func.now(),
            )
        )
        if active_lease is None:
            active_run = None
    if changes_scope:
        if task.stage != TaskStage.SPECIFYING.value:
            await _set_task_state(
                db,
                task=task,
                actor_id=actor.id,
                target=TaskStage.SPECIFYING,
                run_status="PAUSED" if active_run else "QUEUED",
                reason="Clarification changed the accepted specification scope",
                command="specification_scope_changed",
                event_type="task.specification_scope_changed",
            )
        if active_run is not None:
            active_run.cancel_requested_at = datetime.now(UTC)
    elif question.source == "agent" and task.stage == TaskStage.CLARIFYING.value:
        if active_run is not None:
            await _set_task_state(
                db,
                task=task,
                actor_id=actor.id,
                target=TaskStage.IN_PROGRESS,
                run_status="RUNNING",
                reason="Authorized answer resumed the current fenced run",
                command="clarification_resumed",
                event_type="task.clarification_resumed",
                run_id=question.run_id,
            )
        else:
            await _set_task_state(
                db,
                task=task,
                actor_id=actor.id,
                target=TaskStage.SPECIFYING,
                run_status="QUEUED",
                reason="Authorized answer changed the specification; readiness must be reassessed",
                command="clarification_recovery_reassess",
                event_type="task.clarification_recovery_reassessing",
            )
            # The answer adds a durable artifact revision. Re-evaluate that exact revision
            # against the frozen repository base before allowing checkpoint recovery.
            interview, _evaluation = await evaluate_readiness(
                db,
                interview_id=interview.id,
                actor=actor,
                recovery_question=question,
            )
    elif task.stage == TaskStage.READY.value:
        await transition_task(
            db,
            task_id=task.id,
            actor=actor,
            target=TaskStage.SPECIFYING,
            expected_version=task.version,
            reason="An answer changed the OpenSpec revision and readiness must be reassessed",
            return_stage=None,
            command_key=f"spec-answer:{interview.id}:{interview.revision}",
        )
    await db.commit()
    await db.refresh(answer_row)
    await db.refresh(interview)
    return answer_row, interview


def _readiness_gates(
    files: dict[str, str], validation: OpenSpecValidation, *, blocking_questions: int,
    base_current: bool, signoff_required: bool, signoff_received: bool,
    model_route_ready: bool = True,
) -> tuple[float, dict[str, Any]]:
    proposal = files.get("proposal.md", "")
    spec = files.get("spec.md", "")
    design = files.get("design.md", "")
    tasks = files.get("tasks.md", "")
    proposal_checks = {
        "why": bool(re.search(r"(?im)^##\s+Why\s*$", proposal)),
        "what_changes": bool(re.search(r"(?im)^##\s+What Changes\s*$", proposal)),
        "capabilities": bool(re.search(r"(?im)^##\s+Capabilities\s*$", proposal)),
    }
    requirements = re.findall(r"(?im)^###\s+Requirement:?\s+(.+?)\s*$", spec)
    scenarios = re.findall(r"(?im)^####\s+Scenario:\s+(.+?)\s*$", spec)
    scenario_blocks = re.split(r"(?im)^####\s+Scenario:\s+.+$", spec)[1:]
    scenario_checks = bool(requirements and scenarios) and len(scenario_blocks) == len(scenarios) and all(
        re.search(r"(?im)^-\s+\*\*WHEN\*\*\s+.+$", block)
        and re.search(r"(?im)^-\s+\*\*THEN\*\*\s+.+$", block)
        for block in scenario_blocks
    )
    design_checks = {
        "context": bool(re.search(r"(?im)^##\s+Context\s*$", design)),
        "goals": bool(re.search(r"(?im)^##\s+Goals(?:\s+and\s+|\s*/\s*)Non-Goals\s*$", design)),
        "decisions": bool(re.search(r"(?im)^##\s+Decisions\s*$", design)),
        "risks": bool(re.search(r"(?im)^##\s+Risks\s*/\s*Trade-offs\s*$", design)),
    }
    task_checks = bool(re.search(r"(?im)^##\s+Verification\s*$", tasks)) and bool(
        re.search(r"(?m)^-\s+\[ \]\s+.+$", tasks)
    )
    requirement_mapping = bool(requirements) and all(
        requirement.casefold() in tasks.casefold() for requirement in requirements
    )
    sections = [
        1.0 if all(proposal_checks.values()) else sum(proposal_checks.values()) / 3,
        1.0 if scenario_checks else 0.0,
        1.0 if all(design_checks.values()) else sum(design_checks.values()) / 4,
        1.0 if task_checks and requirement_mapping else 0.0,
    ]
    score = round(sections[0] * 0.25 + sections[1] * 0.30 + sections[2] * 0.25 + sections[3] * 0.20, 4)
    gates: dict[str, Any] = {
        "rubric": {
            "proposal": {"weight": 0.25, "score": sections[0], "checks": proposal_checks},
            "scenarios": {"weight": 0.30, "score": sections[1], "requirements": requirements,
                          "scenarios": scenarios, "when_then_present": scenario_checks},
            "design": {"weight": 0.25, "score": sections[2], "checks": design_checks},
            "tasks": {"weight": 0.20, "score": sections[3], "verification_section": task_checks,
                      "requirement_mapping": requirement_mapping},
        },
        "openspec_validation": {"version": validation.version, "valid": validation.valid,
                                "result": validation.result},
        "unresolved_blocking_questions": blocking_questions,
        "base_current": base_current,
        "execution_capabilities": {"model_route_ready": model_route_ready},
        "signoff_required": signoff_required,
        "signoff_received": signoff_received,
    }
    return score, gates


async def evaluate_readiness(
    db: AsyncSession,
    *,
    interview_id: UUID,
    actor: User,
    recovery_question: SpecificationQuestion | None = None,
) -> tuple[SpecificationInterview, SpecificationReadinessEvaluation]:
    interview, repository, task, _membership = await _interview_for_update(db, interview_id, actor)
    try:
        current_base = await _workspace_manager.current_base(repository)
        workspace = await _workspace_manager.open(
            interview.id,
            repository,
            change_id=interview.change_id,
            base_sha=interview.base_sha,
            head_sha=interview.head_sha,
        )
        files = await _workspace_manager.read_bundle(workspace, interview.head_sha)
        validation = await _workspace_manager.validate(workspace)
    except SpecificationWorkspaceError as exc:
        raise HTTPException(503, str(exc)) from exc
    blocking_count = await db.scalar(
        select(__import__("sqlalchemy").func.count())
        .select_from(SpecificationQuestion)
        .where(
            SpecificationQuestion.interview_id == interview.id,
            SpecificationQuestion.classification == "blocking",
            SpecificationQuestion.status == "open",
        )
    ) or 0
    snapshot = interview.readiness_snapshot or {}
    signed_off_by = snapshot.get("signed_off_by")
    signoff_received = isinstance(signed_off_by, str)
    model_route_ready = (
        settings.acp_execution_enabled
        and settings.acp_model_broker_enabled
        and bool(settings.model_gateway_url.strip())
    )
    project = await db.get(Project, interview.project_id) if model_route_ready else None
    if project is not None:
        budget_policy = (
            await db.execute(
                select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project.id)
            )
        ).scalars().first()
        # The broker estimates one input token per serialized request byte and
        # the gateway reserves output separately. Require a route that can fit
        # the configured request-byte bound plus ACP's output ceiling.
        minimum_context = min(
            2_000_000,
            settings.acp_model_max_request_bytes + settings.acp_model_max_output_tokens,
        )
        model_route_ready = (
            budget_policy is not None
            and budget_policy.max_output_tokens >= settings.acp_model_max_output_tokens
            and settings.acp_model_max_output_tokens <= 1_000_000
            and await has_compatible_configured_route(
                db,
                organization_id=project.organization_id,
                pool_kind=ModelPoolKind.INTERACTIVE.value,
                alias_name=settings.acp_model_alias,
                requirements=RouteRequirements(
                    protocol=ModelProtocol.CHAT_COMPLETIONS,
                    tools=True,
                    minimum_context=minimum_context,
                    usage_reporting=True,
                    cancellation=True,
                ),
            )
        )
    else:
        model_route_ready = False
    score, gates = _readiness_gates(
        {Path(path).name: content for path, content in files.items()},
        validation,
        blocking_questions=blocking_count,
        base_current=current_base == interview.base_sha,
        signoff_required=settings.openspec_require_signoff,
        signoff_received=signoff_received,
        model_route_ready=model_route_ready,
    )
    ready = (
        score >= 0.90
        and validation.valid
        and blocking_count == 0
        and gates["base_current"] is True
        and gates["execution_capabilities"]["model_route_ready"] is True
        and gates["rubric"]["tasks"]["requirement_mapping"] is True
        and (not settings.openspec_require_signoff or signoff_received)
    )
    state = "READY" if ready else "NOT_READY"
    if current_base != interview.base_sha:
        state = "STALE"
    evaluation = SpecificationReadinessEvaluation(
        id=uuid4(),
        interview_id=interview.id,
        interview_revision=interview.revision,
        artifact_revision=interview.artifact_revision,
        content_hash=interview.artifact_hash,
        base_sha=interview.base_sha,
        policy_revision=_POLICY_REVISION,
        score=score,
        state=state,
        gates=gates,
        evaluated_by=actor.id,
        signed_off_by=UUID(signed_off_by) if signoff_received else None,
    )
    db.add(evaluation)
    interview.readiness_state = state
    interview.readiness_snapshot = {
        **snapshot,
        "evaluation_id": str(evaluation.id),
        "interview_revision": interview.revision,
        "artifact_revision": interview.artifact_revision,
        "content_hash": interview.artifact_hash,
        "base_sha": interview.base_sha,
        "policy_revision": _POLICY_REVISION,
        "score": score,
        "state": state,
        "gates": gates,
        "request_hash": snapshot.get("request_hash"),
    }
    await _emit(
        db,
        task,
        actor.id,
        "specification.readiness_evaluated",
        {
            "interview_id": str(interview.id),
            "evaluation_id": str(evaluation.id),
            "interview_revision": interview.revision,
            "artifact_revision": interview.artifact_revision,
            "content_hash": interview.artifact_hash,
            "base_sha": interview.base_sha,
            "policy_revision": _POLICY_REVISION,
            "score": score,
            "state": state,
        },
    )
    if ready and recovery_question is not None:
        task.blocked_reason = "worker_lost_checkpoint"
        await _emit(
            db,
            task,
            actor.id,
            "task.clarification_recovery_queued",
            {
                "interview_id": str(interview.id),
                "question_id": str(recovery_question.id),
                "artifact_revision": interview.artifact_revision,
                "readiness_evaluation_id": str(evaluation.id),
                "previous_run_id": (
                    str(recovery_question.run_id) if recovery_question.run_id else None
                ),
                "attempt": recovery_question.attempt,
                "fencing_token": recovery_question.fencing_token,
            },
            run_id=recovery_question.run_id,
        )
    await db.flush()
    target = TaskStage.READY if ready else TaskStage.SPECIFYING
    if task.stage != target.value and task.stage in {
        TaskStage.SPECIFYING.value,
        TaskStage.READY.value,
        TaskStage.CLARIFYING.value,
    }:
        await transition_task(
            db,
            task_id=task.id,
            actor=actor,
            target=target,
            expected_version=task.version,
            reason="Specification readiness gates evaluated",
            return_stage=None,
            command_key=f"spec-readiness:{interview.id}:{interview.revision}:{interview.artifact_hash}",
            run_status=RunStatus.QUEUED if target == TaskStage.READY else None,
        )
    await db.commit()
    await db.refresh(interview)
    await db.refresh(evaluation)
    return interview, evaluation


async def get_readiness(
    db: AsyncSession, *, interview_id: UUID, actor: User
) -> tuple[SpecificationInterview, SpecificationReadinessEvaluation | None]:
    interview, repository, task, _ = await _interview_for_update(
        db, interview_id, actor, operator_only=False, lock=False
    )
    evaluation = await db.scalar(
        select(SpecificationReadinessEvaluation)
        .where(SpecificationReadinessEvaluation.interview_id == interview.id)
        .order_by(SpecificationReadinessEvaluation.created_at.desc())
        .limit(1)
    )
    if evaluation is not None:
        try:
            current_base = await _workspace_manager.current_base(repository)
        except SpecificationWorkspaceError as exc:
            raise HTTPException(503, str(exc)) from exc
        if current_base != evaluation.base_sha or evaluation.content_hash != interview.artifact_hash:
            interview.readiness_state = "STALE"
            if interview.readiness_snapshot is not None:
                interview.readiness_snapshot = {**interview.readiness_snapshot, "state": "STALE"}
            if evaluation.state != "STALE":
                db.add(
                    SpecificationReadinessEvaluation(
                        id=uuid4(),
                        interview_id=interview.id,
                        interview_revision=evaluation.interview_revision,
                        artifact_revision=evaluation.artifact_revision,
                        content_hash=evaluation.content_hash,
                        base_sha=evaluation.base_sha,
                        policy_revision=evaluation.policy_revision,
                        score=evaluation.score,
                        state="STALE",
                        gates={
                            **evaluation.gates,
                            "invalidated": {
                                "base_current": current_base == evaluation.base_sha,
                                "content_hash_current": evaluation.content_hash == interview.artifact_hash,
                            },
                        },
                        evaluated_by=actor.id,
                        signed_off_by=evaluation.signed_off_by,
                    )
                )
            if task.stage == TaskStage.READY.value:
                await record_system_transition(
                    db,
                    task=task,
                    target=TaskStage.SPECIFYING,
                    reason="Specification readiness became stale after source or base drift",
                    event_type="specification.readiness_invalidated",
                    payload={
                        "interview_id": str(interview.id),
                        "base_current": current_base == evaluation.base_sha,
                        "content_hash_current": evaluation.content_hash == interview.artifact_hash,
                    },
                )
            await db.commit()
            await db.refresh(interview)
            evaluation = await db.scalar(
                select(SpecificationReadinessEvaluation)
                .where(SpecificationReadinessEvaluation.interview_id == interview.id)
                .order_by(SpecificationReadinessEvaluation.created_at.desc())
                .limit(1)
            )
    return interview, evaluation


async def sign_off_readiness(
    db: AsyncSession,
    *,
    interview_id: UUID,
    actor: User,
    expected_revision: int,
    expected_artifact_revision: int,
) -> tuple[SpecificationInterview, SpecificationReadinessEvaluation]:
    interview, _, _, membership = await _interview_for_update(
        db, interview_id, actor, operator_only=False
    )
    if membership.role not in {"administrator", "reviewer"}:
        raise HTTPException(403, "Readiness sign-off requires reviewer or administrator role")
    if (
        interview.revision != expected_revision
        or interview.artifact_revision != expected_artifact_revision
    ):
        raise HTTPException(409, "Sign-off references a stale specification revision")
    snapshot = interview.readiness_snapshot or {}
    interview.readiness_snapshot = {
        **snapshot,
        "signed_off_by": str(actor.id),
        "signed_off_at": datetime.now(UTC).isoformat(),
    }
    await db.commit()
    return await evaluate_readiness(db, interview_id=interview_id, actor=actor)


async def generate_architect_draft(
    db: AsyncSession,
    *,
    interview_id: UUID,
    actor: User,
    expected_revision: int,
    authorize_model_generation: bool,
    idempotency_key: str,
) -> tuple[SpecificationInterview, list[SpecificationQuestion]]:
    if not authorize_model_generation:
        raise HTTPException(403, "Set authorize_model_generation=true to authorize this bounded model call")
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    interview, repository, task, _ = await _interview_for_update(db, interview_id, actor)
    if interview.architect_source_revision == expected_revision:
        if interview.architect_idempotency_key != idempotency_key:
            raise HTTPException(409, "Architect revision was already generated with another key")
        questions = list(
            (await db.scalars(select(SpecificationQuestion).where(
                SpecificationQuestion.interview_id == interview.id,
                SpecificationQuestion.source == "architect",
                SpecificationQuestion.source_revision == interview.architect_artifact_revision,
            ))).all()
        )
        if questions:
            return interview, questions
    if interview.revision != expected_revision:
        raise HTTPException(409, "Architect request references a stale specification revision")
    try:
        search = await _workspace_manager.search(
            repository.target_id, repository.default_branch, interview.topic, limit=8
        )
        project = await db.get(Project, interview.project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        draft = await _architect.generate(
            db,
            organization_id=project.organization_id,
            project_id=project.id,
            task_id=task.id,
            interview_id=interview.id,
            revision=interview.revision,
            topic=interview.topic,
            change_id=interview.change_id,
            citations=_citations_payload(search),
        )
    except (SpecificationWorkspaceError, SpecificationArchitectError) as exc:
        raise HTTPException(502, str(exc)) from exc
    files = {
        f"openspec/changes/{interview.change_id}/proposal.md": draft.proposal_md,
        f"openspec/changes/{interview.change_id}/specs/{interview.change_id}/spec.md": draft.spec_md,
        f"openspec/changes/{interview.change_id}/design.md": draft.design_md,
        f"openspec/changes/{interview.change_id}/tasks.md": draft.tasks_md,
    }
    await _save_artifact_revision(
        db,
        interview=interview,
        task=task,
        actor=actor,
        repository=repository,
        files=files,
        summary="Architect generated a focused OpenSpec draft",
    )
    citations = _citations_payload(search)
    questions = []
    artifact_names = set(files)
    for item in draft.questions:
        affected = [path for path in item.affected_artifacts if path in artifact_names]
        if not affected:
            affected = [f"openspec/changes/{interview.change_id}/specs/{interview.change_id}/spec.md"]
        question = SpecificationQuestion(
            interview_id=interview.id,
            task_id=task.id,
            source="architect",
            source_revision=interview.revision,
            classification=item.classification,
            status="open",
            question=item.question,
            proposed_assumption=item.proposed_assumption,
            recommended_options=[option.model_dump() for option in item.recommended_options],
            affected_artifacts=affected,
            evidence=citations,
        )
        db.add(question)
        questions.append(question)
    interview.architect_idempotency_key = idempotency_key
    interview.architect_source_revision = expected_revision
    interview.architect_artifact_revision = interview.artifact_revision
    await _emit(
        db,
        task,
        actor.id,
        "specification.architect_drafted",
        {
            "interview_id": str(interview.id),
            "artifact_revision": interview.artifact_revision,
            "content_hash": interview.artifact_hash,
            "question_count": len(questions),
        },
    )
    await db.commit()
    await db.refresh(interview)
    for question in questions:
        await db.refresh(question)
    return interview, questions
