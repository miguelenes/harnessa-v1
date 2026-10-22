"""Authenticated OpenSpec interview, retrieval, artifact, and readiness APIs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from harnessa.auth.users import current_active_user
from harnessa.contracts import (
    RepositoryCitation,
    RepositorySearchRequest,
    RepositorySearchResponse,
    SpecificationAnswerCreate,
    SpecificationAnswerRead,
    SpecificationAnswerResult,
    SpecificationArchitectRead,
    SpecificationArchitectRequest,
    SpecificationArtifactRead,
    SpecificationArtifactUpdate,
    SpecificationExplorerEntry,
    SpecificationExplorerRead,
    SpecificationInterviewCreate,
    SpecificationInterviewRead,
    SpecificationQuestionRead,
    SpecificationReadinessRead,
    SpecificationReadinessSignoff,
)
from harnessa.db.models import User
from harnessa.db.session import get_db
from harnessa.db.specification import (
    SpecificationInterview,
    SpecificationQuestion,
    SpecificationReadinessEvaluation,
)
from harnessa.services.authorization import require_membership
from harnessa.services.specifications import (
    answer_question,
    create_interview,
    evaluate_readiness,
    generate_architect_draft,
    get_readiness,
    list_explorer_entries,
    read_artifacts,
    search_repository,
    sign_off_readiness,
    update_artifacts,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["specifications"])


def _interview_read(row: SpecificationInterview) -> SpecificationInterviewRead:
    return SpecificationInterviewRead(
        id=row.id,
        task_id=row.task_id,
        project_id=row.project_id,
        repository_id=row.repository_id,
        topic=row.topic,
        revision=row.revision,
        artifact_revision=row.artifact_revision,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        artifact_hash=row.artifact_hash,
        readiness_state=row.readiness_state,
        policy_revision=row.policy_revision,
    )


def _readiness_read(
    row: SpecificationInterview, evaluation: SpecificationReadinessEvaluation | None
) -> SpecificationReadinessRead:
    if evaluation is not None:
        return SpecificationReadinessRead(
            interview_id=row.id,
            interview_revision=evaluation.interview_revision,
            artifact_revision=evaluation.artifact_revision,
            content_hash=evaluation.content_hash,
            base_sha=evaluation.base_sha,
            policy_revision=evaluation.policy_revision,
            score=float(evaluation.score),
            state=row.readiness_state,
            gates=evaluation.gates,
            signed_off_by=evaluation.signed_off_by,
        )
    return SpecificationReadinessRead(
        interview_id=row.id,
        interview_revision=row.revision,
        artifact_revision=row.artifact_revision,
        content_hash=row.artifact_hash,
        base_sha=row.base_sha,
        policy_revision=row.policy_revision,
        score=0,
        state=row.readiness_state,
        gates={},
    )


async def _project_interview(
    db: AsyncSession, actor: User, project_id: UUID, interview_id: UUID
) -> SpecificationInterview:
    await require_membership(db, actor, project_id)
    row = await db.get(SpecificationInterview, interview_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(404, "Specification interview not found")
    return row


@router.post(
    "/projects/{project_id}/specification-interviews",
    response_model=SpecificationInterviewRead,
    status_code=201,
)
async def start_interview(
    project_id: UUID,
    body: SpecificationInterviewCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationInterviewRead:
    row, replayed = await create_interview(
        db,
        project_id=project_id,
        repository_id=body.repository_id,
        actor=actor,
        topic=body.topic,
        idempotency_key=idempotency_key,
    )
    if replayed:
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    else:
        row, _ = await evaluate_readiness(db, interview_id=row.id, actor=actor)
    return _interview_read(row)


@router.get(
    "/projects/{project_id}/specification-interviews/{interview_id}",
    response_model=SpecificationInterviewRead,
)
async def get_interview(
    project_id: UUID,
    interview_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationInterviewRead:
    return _interview_read(await _project_interview(db, actor, project_id, interview_id))


@router.get(
    "/projects/{project_id}/specification-interviews/{interview_id}/questions",
    response_model=list[SpecificationQuestionRead],
)
async def list_questions(
    project_id: UUID,
    interview_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[SpecificationQuestionRead]:
    row = await _project_interview(db, actor, project_id, interview_id)
    questions = await db.scalars(
        select(SpecificationQuestion)
        .where(SpecificationQuestion.interview_id == row.id)
        .order_by(SpecificationQuestion.created_at, SpecificationQuestion.id)
    )
    return [SpecificationQuestionRead.model_validate(question) for question in questions]


@router.post(
    "/projects/{project_id}/specification-interviews/{interview_id}/questions/{question_id}/answers",
    response_model=SpecificationAnswerResult,
)
async def submit_answer(
    project_id: UUID,
    interview_id: UUID,
    question_id: UUID,
    body: SpecificationAnswerCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationAnswerResult:
    await _project_interview(db, actor, project_id, interview_id)
    answer, interview = await answer_question(
        db,
        interview_id=interview_id,
        question_id=question_id,
        actor=actor,
        expected_revision=body.expected_revision,
        answer=body.answer,
        idempotency_key=idempotency_key,
        changes_scope=body.changes_scope,
    )
    response.headers["ETag"] = f'W/"{interview.revision}"'
    return SpecificationAnswerResult(
        answer=SpecificationAnswerRead.model_validate(answer),
        interview=_interview_read(interview),
    )


@router.get(
    "/projects/{project_id}/specification-interviews/{interview_id}/artifacts",
    response_model=SpecificationArtifactRead,
)
async def get_artifacts(
    project_id: UUID,
    interview_id: UUID,
    response: Response,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationArtifactRead:
    row = await _project_interview(db, actor, project_id, interview_id)
    row, files, diff = await read_artifacts(db, interview_id=row.id, actor=actor)
    response.headers["ETag"] = f'W/"{row.revision}"'
    return SpecificationArtifactRead(
        interview_id=row.id,
        revision=row.artifact_revision,
        base_sha=row.base_sha,
        commit_sha=row.head_sha,
        content_hash=row.artifact_hash,
        paths=list(files),
        change_summary="Current immutable OpenSpec revision",
        diff=diff,
        proposal_md=next(value for path, value in files.items() if path.endswith("/proposal.md")),
        spec_md=next(value for path, value in files.items() if path.endswith("/spec.md")),
        design_md=next(value for path, value in files.items() if path.endswith("/design.md")),
        tasks_md=next(value for path, value in files.items() if path.endswith("/tasks.md")),
    )


@router.put(
    "/projects/{project_id}/specification-interviews/{interview_id}/artifacts",
    response_model=SpecificationInterviewRead,
)
async def put_artifacts(
    project_id: UUID,
    interview_id: UUID,
    body: SpecificationArtifactUpdate,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationInterviewRead:
    await _project_interview(db, actor, project_id, interview_id)
    if if_match.strip().removeprefix("W/").strip('"') != str(body.expected_revision):
        raise HTTPException(412, "If-Match does not match the artifact revision")
    row = await update_artifacts(
        db,
        interview_id=interview_id,
        actor=actor,
        expected_revision=body.expected_revision,
        proposal_md=body.proposal_md,
        spec_md=body.spec_md,
        design_md=body.design_md,
        tasks_md=body.tasks_md,
    )
    response.headers["ETag"] = f'W/"{row.revision}"'
    return _interview_read(row)


@router.get(
    "/projects/{project_id}/specification-interviews/{interview_id}/readiness",
    response_model=SpecificationReadinessRead,
)
async def read_readiness(
    project_id: UUID,
    interview_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationReadinessRead:
    await _project_interview(db, actor, project_id, interview_id)
    row, evaluation = await get_readiness(db, interview_id=interview_id, actor=actor)
    return _readiness_read(row, evaluation)


@router.post(
    "/projects/{project_id}/specification-interviews/{interview_id}/readiness/evaluate",
    response_model=SpecificationReadinessRead,
)
async def evaluate_interview_readiness(
    project_id: UUID,
    interview_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationReadinessRead:
    await _project_interview(db, actor, project_id, interview_id)
    row, evaluation = await evaluate_readiness(db, interview_id=interview_id, actor=actor)
    return _readiness_read(row, evaluation)


@router.post(
    "/projects/{project_id}/specification-interviews/{interview_id}/readiness/sign-off",
    response_model=SpecificationReadinessRead,
)
async def sign_off_interview_readiness(
    project_id: UUID,
    interview_id: UUID,
    body: SpecificationReadinessSignoff,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationReadinessRead:
    await _project_interview(db, actor, project_id, interview_id)
    row, evaluation = await sign_off_readiness(
        db,
        interview_id=interview_id,
        actor=actor,
        expected_revision=body.expected_revision,
        expected_artifact_revision=body.expected_artifact_revision,
    )
    return _readiness_read(row, evaluation)


@router.post(
    "/projects/{project_id}/specification-interviews/{interview_id}/architect",
    response_model=SpecificationArchitectRead,
)
async def run_architect(
    project_id: UUID,
    interview_id: UUID,
    body: SpecificationArchitectRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationArchitectRead:
    await _project_interview(db, actor, project_id, interview_id)
    row, questions = await generate_architect_draft(
        db,
        interview_id=interview_id,
        actor=actor,
        expected_revision=body.expected_revision,
        authorize_model_generation=body.authorize_model_generation,
        idempotency_key=idempotency_key,
    )
    row, _ = await evaluate_readiness(db, interview_id=row.id, actor=actor)
    return SpecificationArchitectRead(
        interview_id=row.id,
        revision=row.revision,
        artifact_revision=row.artifact_revision,
        questions=[SpecificationQuestionRead.model_validate(question) for question in questions],
    )


@router.post(
    "/projects/{project_id}/repositories/{repository_id}/specification-search",
    response_model=RepositorySearchResponse,
)
async def repository_search(
    project_id: UUID,
    repository_id: UUID,
    body: RepositorySearchRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> RepositorySearchResponse:
    result = await search_repository(
        db,
        project_id=project_id,
        repository_id=repository_id,
        actor=actor,
        query=body.query,
        limit=body.limit,
    )
    return RepositorySearchResponse(
        revision=result.revision,
        results=[
            RepositoryCitation(
                revision=item.revision,
                path=item.path,
                line=item.line,
                snippet=item.snippet,
                matched_terms=list(item.matched_terms),
                untrusted_source_content=True,
            )
            for item in result.citations
        ],
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}/specification-explorer",
    response_model=SpecificationExplorerRead,
)
async def specification_explorer(
    project_id: UUID,
    repository_id: UUID,
    limit: int = Query(default=500, ge=1, le=500),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationExplorerRead:
    revision, entries = await list_explorer_entries(
        db,
        project_id=project_id,
        repository_id=repository_id,
        actor=actor,
    )
    return SpecificationExplorerRead(
        revision=revision,
        entries=[SpecificationExplorerEntry(**item) for item in entries[:limit]],
    )
