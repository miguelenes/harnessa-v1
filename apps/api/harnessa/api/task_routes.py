"""Project-scoped task, run, recovery, and durable event APIs."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    DeadLetterRead,
    DispatchRebuildResult,
    EventEnvelope,
    EventReplayItem,
    EventReplayPage,
    RecoveryRequest,
    RunRead,
    TaskCommandRequest,
    TaskCreate,
    TaskRead,
    TaskTransitionRead,
    TaskTransitionRequest,
)
from harnessa.db.models import Task, User
from harnessa.db.orchestration import DeadLetter, DomainEvent, Run, TaskTransition
from harnessa.db.session import get_db
from harnessa.services.authorization import require_membership
from harnessa.services.lifecycle import command_task, create_task, transition_task
from harnessa.services.recovery import replay_dead_letter
from harnessa.services.streams import rebuild_dispatch_stream
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _if_match(raw: str) -> int:
    value = raw.strip().removeprefix("W/").strip('"')
    try:
        version = int(value)
    except ValueError as exc:
        raise HTTPException(400, "If-Match must contain the observed task version") from exc
    if version < 1:
        raise HTTPException(400, "If-Match task version must be positive")
    return version


@router.post("/projects/{project_id}/tasks", response_model=TaskRead, status_code=201)
async def create_project_task(
    project_id: UUID,
    body: TaskCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task, replayed = await create_task(
        db,
        project_id=project_id,
        actor=actor,
        title=body.title,
        description=body.description,
        priority=body.priority,
        command_key=idempotency_key,
    )
    if replayed:
        response.status_code = 200
        response.headers["Idempotency-Replayed"] = "true"
    return TaskRead.model_validate(task)


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead])
async def list_project_tasks(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    before: UUID | None = None,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[TaskRead]:
    await require_membership(db, actor, project_id)
    query = select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc(), Task.id)
    if before:
        cursor = await db.get(Task, before)
        if cursor is None or cursor.project_id != project_id:
            raise HTTPException(400, "Task cursor is not in this project")
        query = query.where(Task.created_at < cursor.created_at)
    tasks = (await db.execute(query.limit(limit))).scalars().all()
    return [TaskRead.model_validate(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    return TaskRead.model_validate(task)


@router.post("/tasks/{task_id}/transition", response_model=TaskRead)
async def transition(
    task_id: UUID,
    body: TaskTransitionRequest,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task, replayed = await transition_task(
        db,
        task_id=task_id,
        actor=actor,
        target=body.target_stage,
        expected_version=_if_match(if_match),
        reason=body.reason,
        return_stage=body.return_stage,
        command_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return TaskRead.model_validate(task)


@router.post("/tasks/{task_id}/commands", response_model=TaskRead)
async def command(
    task_id: UUID,
    body: TaskCommandRequest,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task, replayed = await command_task(
        db,
        task_id=task_id,
        actor=actor,
        command=body.command,
        expected_version=_if_match(if_match),
        reason=body.reason,
        command_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return TaskRead.model_validate(task)


@router.get("/tasks/{task_id}/runs", response_model=list[RunRead])
async def list_runs(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[RunRead]:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    runs = (await db.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt))).scalars()
    return [RunRead.model_validate(run) for run in runs]


@router.get("/tasks/{task_id}/transitions", response_model=list[TaskTransitionRead])
async def list_transitions(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[TaskTransitionRead]:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    rows = (
        await db.execute(
            select(TaskTransition)
            .where(TaskTransition.task_id == task_id)
            .order_by(TaskTransition.version_after)
        )
    ).scalars().all()
    return [TaskTransitionRead.model_validate(row) for row in rows]


@router.get("/projects/{project_id}/events", response_model=EventReplayPage)
async def replay_events(
    project_id: UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EventReplayPage:
    await require_membership(db, actor, project_id)
    if limit > settings.event_replay_limit:
        raise HTTPException(422, f"Replay limit is {settings.event_replay_limit}")
    now = datetime.now(UTC)
    oldest = await db.scalar(
        select(DomainEvent.project_sequence)
        .where(DomainEvent.project_id == project_id, DomainEvent.retention_until > now)
        .order_by(DomainEvent.project_sequence)
        .limit(1)
    )
    if oldest is not None and after < oldest - 1:
        raise HTTPException(
            410,
            detail={"code": "event_cursor_expired", "oldest_available_cursor": oldest - 1},
        )
    events = (
        await db.execute(
            select(DomainEvent)
            .where(
                DomainEvent.project_id == project_id,
                DomainEvent.project_sequence > after,
                DomainEvent.retention_until > now,
            )
            .order_by(DomainEvent.project_sequence)
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(events) > limit
    items = events[:limit]
    envelopes = [
        EventReplayItem(
            cursor=item.project_sequence,
            event=EventEnvelope(
            event_id=item.id,
            event_type=item.event_type,
            event_version=item.event_version,
            schema_version=item.schema_version,
            organization_id=item.organization_id,
            project_id=item.project_id,
            task_id=item.task_id,
            run_id=item.run_id,
            sequence=item.aggregate_sequence,
            actor_id=item.actor_id,
            occurred_at=item.created_at,
            correlation_id=item.correlation_id,
            causation_id=item.causation_id,
            payload=item.payload,
            ),
        )
        for item in items
    ]
    return EventReplayPage(
        items=envelopes,
        next_cursor=items[-1].project_sequence if has_more and items else None,
        replay_limit=settings.event_replay_limit,
    )


@router.get("/projects/{project_id}/dead-letters", response_model=list[DeadLetterRead])
async def list_dead_letters(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeadLetterRead]:
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Only project operators can inspect dead letters")
    rows = (
        await db.execute(
            select(DeadLetter)
            .where(DeadLetter.project_id == project_id)
            .order_by(DeadLetter.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return [DeadLetterRead.model_validate(row) for row in rows]


@router.post("/dead-letters/{dead_letter_id}/replay", response_model=TaskRead)
async def replay_dead_letter_route(
    dead_letter_id: UUID,
    body: RecoveryRequest,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task, replayed = await replay_dead_letter(
        db,
        dead_letter_id=dead_letter_id,
        actor=actor,
        reason=body.reason,
        command_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return TaskRead.model_validate(task)


@router.post("/ops/dispatch/rebuild", response_model=DispatchRebuildResult)
async def rebuild_dispatch(
    request: Request,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DispatchRebuildResult:
    membership = await require_membership(db, actor)
    if membership.role != "administrator":
        raise HTTPException(403, "Only administrators can rebuild dispatch streams")
    rebuilt = await rebuild_dispatch_stream(request.app.state.redis)
    return DispatchRebuildResult(rebuilt=rebuilt)
