"""Project-scoped task, run, recovery, and durable event APIs."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from ag_ui.core import CustomEvent, EventType
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from harnessa.adapters.ui_events import project_domain_event
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    DeadLetterRead,
    DispatchRebuildResult,
    EventEnvelope,
    EventReplayItem,
    EventReplayPage,
    RecoveryRequest,
    Role,
    RunRead,
    SpecificationInterviewRead,
    TaskCapabilitiesRead,
    TaskCommand,
    TaskCommandRequest,
    TaskCreate,
    TaskRead,
    TaskStage,
    TaskTransitionRead,
    TaskTransitionRequest,
)
from harnessa.db.models import Project, Task, User
from harnessa.db.orchestration import DeadLetter, DomainEvent, Run, TaskTransition
from harnessa.db.session import get_db
from harnessa.db.specification import SpecificationInterview
from harnessa.domain.lifecycle import allowed_transition_targets
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


async def _expired_cursor_boundary(
    db: AsyncSession, project_id: UUID, after: int, now: datetime
) -> int | None:
    oldest = await db.scalar(
        select(DomainEvent.project_sequence)
        .where(DomainEvent.project_id == project_id, DomainEvent.retention_until > now)
        .order_by(DomainEvent.project_sequence)
        .limit(1)
    )
    if oldest is not None:
        return oldest - 1 if after < oldest - 1 else None
    latest = await db.scalar(select(Project.event_sequence).where(Project.id == project_id))
    return latest if latest is not None and after < latest else None


def _allowed_transitions(
    current: TaskStage, role: Role, run_status: str
) -> frozenset[TaskStage]:
    # Match transition_task guards so advertised actions are directly executable.
    candidates = allowed_transition_targets(current) - {
        TaskStage.IN_PROGRESS,
        TaskStage.RETRY_EXHAUSTED,
        TaskStage.CANCELLED,
    }
    if current == TaskStage.IN_PROGRESS:
        candidates -= {TaskStage.READY}  # checkpoint recovery is a command path
    if run_status != "SUCCEEDED":
        candidates -= {TaskStage.VERIFYING, TaskStage.LEARNING, TaskStage.HARNESS_PROBE}
    if role == Role.ADMINISTRATOR:
        return candidates
    if role == Role.REVIEWER:
        return (
            candidates & {TaskStage.LEARNING, TaskStage.REJECTED, TaskStage.IN_PROGRESS}
            if current == TaskStage.HUMAN_REVIEW
            else frozenset()
        )
    if role == Role.OPERATOR:
        return frozenset(
            target
            for target in candidates
            if current != TaskStage.HUMAN_REVIEW
            and target not in {TaskStage.LEARNING, TaskStage.DONE}
            and not (target == TaskStage.REJECTED and current != TaskStage.BACKLOG)
        )
    return frozenset()


def _task_read(
    task: Task,
    role: Role,
    interview: SpecificationInterview | None = None,
) -> TaskRead:
    base = TaskRead.model_validate(task).model_dump(
        exclude={
            "allowed_transitions",
            "specification_readiness_state",
            "specification_revision",
        }
    )
    return TaskRead(
        **base,
        allowed_transitions=sorted(
            _allowed_transitions(TaskStage(task.stage), role, task.run_status),
            key=lambda stage: stage.value,
        ),
        specification_readiness_state=interview.readiness_state if interview else None,
        specification_revision=interview.revision if interview else None,
    )


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
    membership = await require_membership(db, actor, project_id)
    return _task_read(task, Role(membership.role))


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead])
async def list_project_tasks(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    before: UUID | None = None,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[TaskRead]:
    membership = await require_membership(db, actor, project_id)
    query = select(Task).where(Task.project_id == project_id).order_by(Task.created_at.desc(), Task.id)
    if before:
        cursor = await db.get(Task, before)
        if cursor is None or cursor.project_id != project_id:
            raise HTTPException(400, "Task cursor is not in this project")
        query = query.where(Task.created_at < cursor.created_at)
    tasks = (await db.execute(query.limit(limit))).scalars().all()
    interviews = (
        await db.execute(
            select(SpecificationInterview).where(
                SpecificationInterview.task_id.in_([task.id for task in tasks])
            )
        )
    ).scalars().all() if tasks else []
    by_task = {row.task_id: row for row in interviews}
    return [_task_read(task, Role(membership.role), by_task.get(task.id)) for task in tasks]


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    return _task_read(task, Role(membership.role))


@router.get("/tasks/{task_id}/capabilities", response_model=TaskCapabilitiesRead)
async def task_capabilities(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> TaskCapabilitiesRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    role = Role(membership.role)
    transitions = _allowed_transitions(TaskStage(task.stage), role, task.run_status)
    run_commands: list[TaskCommand] = []
    if role in {"administrator", "operator"}:
        if task.run_status in {"QUEUED", "RUNNING"}:
            run_commands.append(TaskCommand.PAUSE)
        if task.run_status == "PAUSED":
            run_commands.append(TaskCommand.RESUME)
        if (
            task.run_status not in {"SUCCEEDED", "CANCELLED"}
            and task.stage not in {"DONE", "REJECTED", "CANCELLED"}
        ):
            run_commands.append(TaskCommand.CANCEL)
        if task.run_status == "FAILED":
            run_commands.append(TaskCommand.RETRY)
    can_mutate = role in {"administrator", "operator"}
    return TaskCapabilitiesRead(
        role=role,
        allowed_transitions=sorted(transitions, key=lambda stage: stage.value),
        allowed_commands=run_commands,
        can_answer_questions=can_mutate,
        can_edit_artifacts=can_mutate,
    )


@router.get(
    "/tasks/{task_id}/specification-interview", response_model=SpecificationInterviewRead
)
async def task_specification_interview(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SpecificationInterviewRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    await require_membership(db, actor, task.project_id)
    row = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == task_id)
    )
    if row is None:
        raise HTTPException(404, "Task has no specification interview")
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
    membership = await require_membership(db, actor, task.project_id)
    return _task_read(task, Role(membership.role))


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
    membership = await require_membership(db, actor, task.project_id)
    return _task_read(task, Role(membership.role))


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
    expired_boundary = await _expired_cursor_boundary(db, project_id, after, now)
    if expired_boundary is not None:
        raise HTTPException(
            410,
            detail={"code": "event_cursor_expired", "oldest_available_cursor": expired_boundary},
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


@router.get("/projects/{project_id}/events/stream")
async def stream_project_events(
    project_id: UUID,
    request: Request,
    after: int = Query(default=0, ge=0),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Authenticated durable replay + tail. Closing the response only ends this reader."""
    await require_membership(db, actor, project_id)
    now = datetime.now(UTC)
    expired_boundary = await _expired_cursor_boundary(db, project_id, after, now)
    if expired_boundary is not None:
        raise HTTPException(
            410,
            detail={"code": "event_cursor_expired", "oldest_available_cursor": expired_boundary},
        )
    await db.commit()
    encoder = EventEncoder()

    async def events() -> AsyncIterator[str]:
        nonlocal after
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            try:
                await require_membership(db, actor, project_id)
                await db.commit()
            except HTTPException:
                # The connection may already be committed; notify the UI and close it.
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="harnessa.access_revoked",
                        value={},
                    )
                )
                return
            now = datetime.now(UTC)
            current_boundary = await _expired_cursor_boundary(db, project_id, after, now)
            if current_boundary is not None:
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="harnessa.cursor_expired",
                        value={"oldestAvailableCursor": current_boundary},
                    )
                )
                return
            batch = (
                await db.execute(
                    select(DomainEvent)
                    .where(
                        DomainEvent.project_id == project_id,
                        DomainEvent.project_sequence > after,
                        DomainEvent.retention_until > now,
                    )
                    .order_by(DomainEvent.project_sequence)
                    .limit(min(settings.event_replay_limit, 100))
                )
            ).scalars().all()
            await db.commit()
            if batch:
                for row in batch:
                    after = row.project_sequence
                    yield f"id: {after}\n{encoder.encode(project_domain_event(row))}"
                continue
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
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
