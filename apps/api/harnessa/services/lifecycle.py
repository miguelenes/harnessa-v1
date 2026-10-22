"""Atomic task commands: authorization, lifecycle guards, audit, and outbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.config import settings
from harnessa.contracts import RunStatus, TaskCommand, TaskStage
from harnessa.db.models import Project, Task, User
from harnessa.db.orchestration import DomainEvent, OutboxMessage, Run, TaskTransition
from harnessa.domain.lifecycle import (
    InvalidTaskTransition,
    TaskVersionConflict,
    TransitionGuardFailed,
    assert_transition,
)
from harnessa.services.authorization import require_membership


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_role(role: str, current: TaskStage, target: TaskStage, *, command: str) -> None:
    if role == "administrator":
        return
    if command in {"pause", "resume", "cancel", "retry"}:
        if role != "operator":
            raise HTTPException(403, "Only project operators can issue run commands")
        return
    if role == "reviewer":
        if current == TaskStage.HUMAN_REVIEW and target in {
            TaskStage.LEARNING,
            TaskStage.REJECTED,
            TaskStage.IN_PROGRESS,
        }:
            return
        raise HTTPException(403, "Reviewers can only decide tasks in human review")
    if role != "operator":
        raise HTTPException(403, "Insufficient project role")
    if current == TaskStage.HUMAN_REVIEW or target in {TaskStage.LEARNING, TaskStage.DONE}:
        raise HTTPException(403, "Human review decisions require a reviewer or administrator")
    if target == TaskStage.REJECTED and current != TaskStage.BACKLOG:
        raise HTTPException(403, "Rejecting an active task requires administrator authority")


async def _record_event(
    db: AsyncSession,
    *,
    task: Task,
    event_type: str,
    actor_id: UUID | None,
    payload: dict[str, Any],
    run_id: UUID | None = None,
) -> DomainEvent:
    project = await db.scalar(select(Project).where(Project.id == task.project_id).with_for_update())
    if project is None:
        raise HTTPException(404, "Project not found")
    project.event_sequence += 1
    event = DomainEvent(
        id=uuid4(),
        event_type=event_type,
        event_version=1,
        schema_version=1,
        organization_id=project.organization_id,
        project_id=task.project_id,
        task_id=task.id,
        run_id=run_id,
        aggregate_sequence=task.version,
        project_sequence=project.event_sequence,
        actor_id=actor_id,
        payload=payload,
        retention_until=datetime.now(timezone.utc)
        + timedelta(days=settings.event_retention_days),
    )
    db.add(event)
    db.add(OutboxMessage(event_id=event.id, attempts=0))
    return event


async def record_system_transition(
    db: AsyncSession,
    *,
    task: Task,
    target: TaskStage,
    reason: str,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    recovery: bool = False,
    run_status: RunStatus | None = None,
) -> None:
    """Apply an internal transition within the caller's transaction."""
    current = TaskStage(task.stage)
    assert_transition(
        current,
        target,
        expected_version=task.version,
        current_version=task.version,
        reason=reason,
        return_stage=TaskStage(task.return_stage) if task.return_stage else None,
        recovery=recovery,
    )
    before = task.version
    old_run_status = task.run_status
    task.stage = target.value
    if run_status is not None:
        task.run_status = run_status.value
    task.version += 1
    db.add(
        TaskTransition(
            task_id=task.id,
            project_id=task.project_id,
            actor_id=None,
            from_stage=current.value,
            to_stage=target.value,
            from_run_status=old_run_status,
            to_run_status=task.run_status,
            reason=reason,
            command="system_transition",
            version_before=before,
            version_after=task.version,
        )
    )
    event_payload = {"from_stage": current.value, "to_stage": target.value,
                     "reason": reason, "version": task.version,
                     "from_run_status": old_run_status,
                     "run_status": task.run_status}
    if payload:
        event_payload.update(payload)
    await _record_event(
        db,
        task=task,
        event_type=event_type or ("task.ready" if target == TaskStage.READY else "task.transitioned"),
        actor_id=None,
        run_id=run_id,
        payload=event_payload,
    )


async def _deduplicated_command(
    db: AsyncSession, *, actor_id: UUID, command_key: str, request_hash: str
) -> Task | None:
    previous = await db.scalar(
        select(TaskTransition).where(
            TaskTransition.actor_id == actor_id,
            TaskTransition.command_key == command_key,
        )
    )
    if previous is None:
        return None
    if previous.request_hash != request_hash:
        raise HTTPException(409, "Idempotency-Key was already used with a different task command")
    task = await db.get(Task, previous.task_id)
    if task is None:
        raise HTTPException(409, "The original command's task is no longer available")
    return task


async def _lock_command_key(db: AsyncSession, actor_id: UUID, command_key: str) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"task-command:{actor_id}:{command_key}"},
    )


async def create_task(
    db: AsyncSession,
    *,
    project_id: UUID,
    actor: User,
    title: str,
    description: str,
    priority: int,
    command_key: str,
) -> tuple[Task, bool]:
    if not command_key or len(command_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Only administrators and operators can create tasks")
    request = {"command": "create", "project_id": str(project_id), "title": title,
               "description": description, "priority": priority}
    digest = _digest(request)
    try:
        await _lock_command_key(db, actor.id, command_key)
        replay = await _deduplicated_command(
            db, actor_id=actor.id, command_key=command_key, request_hash=digest
        )
        if replay:
            await require_membership(db, actor, replay.project_id)
            return replay, True
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        task = Task(
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            stage=TaskStage.BACKLOG.value,
            run_status=RunStatus.QUEUED.value,
            version=1,
        )
        db.add(task)
        await db.flush()
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=project_id,
                actor_id=actor.id,
                from_stage=None,
                to_stage=TaskStage.BACKLOG.value,
                from_run_status=None,
                to_run_status=RunStatus.QUEUED.value,
                reason="Task created",
                command="create",
                command_key=command_key,
                request_hash=digest,
                version_before=0,
                version_after=1,
            )
        )
        await _record_event(
            db, task=task, event_type="task.created", actor_id=actor.id,
            payload={"stage": task.stage, "run_status": task.run_status, "version": 1}
        )
        await db.commit()
        return task, False
    except Exception:
        await db.rollback()
        raise


async def transition_task(
    db: AsyncSession,
    *,
    task_id: UUID,
    actor: User,
    target: TaskStage,
    expected_version: int,
    reason: str,
    return_stage: TaskStage | None,
    command_key: str,
    recovery: bool = False,
) -> tuple[Task, bool]:
    if not command_key or len(command_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    request = {"command": "transition", "task_id": str(task_id), "target": target.value,
               "expected_version": expected_version, "reason": reason,
               "return_stage": return_stage.value if return_stage else None}
    digest = _digest(request)
    try:
        await _lock_command_key(db, actor.id, command_key)
        replay = await _deduplicated_command(
            db, actor_id=actor.id, command_key=command_key, request_hash=digest
        )
        if replay:
            await require_membership(db, actor, replay.project_id)
            return replay, True
        task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            raise HTTPException(404, "Task not found")
        membership = await require_membership(db, actor, task.project_id)
        current = TaskStage(task.stage)
        _check_role(membership.role, current, target, command="transition")
        if target in {
            TaskStage.IN_PROGRESS, TaskStage.RETRY_EXHAUSTED, TaskStage.CANCELLED
        }:
            raise TransitionGuardFailed(
                f"{target.value} is reserved for scheduler, retry-policy, or command handling"
            )
        effective_return_stage = return_stage or (
            TaskStage(task.return_stage) if task.return_stage else None
        )
        assert_transition(
            current,
            target,
            expected_version=expected_version,
            current_version=task.version,
            reason=reason,
            return_stage=effective_return_stage,
            recovery=recovery,
        )
        if target == TaskStage.LEARNING and task.run_status != RunStatus.SUCCEEDED.value:
            raise TransitionGuardFailed("Learning requires a successful execution run")
        if target == TaskStage.VERIFYING and task.run_status != RunStatus.SUCCEEDED.value:
            raise TransitionGuardFailed("Verification requires a successful execution run")
        if target == TaskStage.DONE and current != TaskStage.LEARNING:
            raise TransitionGuardFailed("DONE can only follow the learning stage")
        if current in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE}:
            if target not in {TaskStage.CANCELLED, TaskStage.REJECTED} and target != task.return_stage:
                raise TransitionGuardFailed("Requested stage differs from the stored return stage")
        if target in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE}:
            task.return_stage = (effective_return_stage or current).value
        elif current in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE}:
            task.return_stage = None
        task.stage = target.value
        version_before = task.version
        task.version += 1
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=task.project_id,
                actor_id=actor.id,
                from_stage=current.value,
                to_stage=target.value,
                from_run_status=task.run_status,
                to_run_status=task.run_status,
                reason=reason,
                command="transition",
                command_key=command_key,
                request_hash=digest,
                version_before=version_before,
                version_after=task.version,
            )
        )
        await _record_event(
            db, task=task,
            event_type="task.ready" if target == TaskStage.READY else "task.transitioned",
            actor_id=actor.id,
            payload={"from_stage": current.value, "to_stage": target.value,
                     "reason": reason, "version": task.version,
                     "return_stage": task.return_stage}
        )
        await db.commit()
        return task, False
    except TaskVersionConflict as exc:
        await db.rollback()
        raise HTTPException(
            409, detail={"code": "version_conflict", "current_version": exc.current}
        ) from exc
    except (InvalidTaskTransition, TransitionGuardFailed) as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code": "transition_rejected", "reason": str(exc)}) from exc
    except Exception:
        await db.rollback()
        raise


async def command_task(
    db: AsyncSession,
    *,
    task_id: UUID,
    actor: User,
    command: TaskCommand,
    expected_version: int,
    reason: str,
    command_key: str,
) -> tuple[Task, bool]:
    if not command_key or len(command_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    request = {"command": command.value, "task_id": str(task_id),
               "expected_version": expected_version, "reason": reason}
    digest = _digest(request)
    try:
        await _lock_command_key(db, actor.id, command_key)
        replay = await _deduplicated_command(
            db, actor_id=actor.id, command_key=command_key, request_hash=digest
        )
        if replay:
            await require_membership(db, actor, replay.project_id)
            return replay, True
        task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            raise HTTPException(404, "Task not found")
        membership = await require_membership(db, actor, task.project_id)
        _check_role(membership.role, TaskStage(task.stage), TaskStage(task.stage), command=command.value)
        if task.version != expected_version:
            raise TaskVersionConflict(expected_version, task.version)
        before_status = task.run_status
        before_stage = task.stage
        now = datetime.now(timezone.utc)
        active_run = await db.scalar(
            select(Run).where(Run.task_id == task.id, Run.status == RunStatus.RUNNING.value)
            .order_by(Run.attempt.desc()).with_for_update()
        )
        if command == TaskCommand.PAUSE:
            if task.run_status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
                raise HTTPException(409, "Only queued or running tasks can be paused")
            task.run_status = RunStatus.PAUSED.value
            if active_run:
                active_run.cancel_requested_at = now
        elif command == TaskCommand.RESUME:
            if task.run_status != RunStatus.PAUSED.value:
                raise HTTPException(409, "Only paused tasks can be resumed")
            task.run_status = RunStatus.QUEUED.value
            if task.stage == TaskStage.IN_PROGRESS.value:
                task.stage = TaskStage.READY.value
        elif command == TaskCommand.CANCEL:
            if task.run_status in {RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value}:
                raise HTTPException(409, "Task is already terminal")
            if task.stage in {
                TaskStage.DONE.value, TaskStage.REJECTED.value, TaskStage.CANCELLED.value
            }:
                raise HTTPException(409, "Task lifecycle is already terminal")
            task.run_status = RunStatus.CANCELLED.value
            task.cancel_requested_at = now
            if active_run:
                active_run.cancel_requested_at = now
        elif command == TaskCommand.RETRY:
            if task.run_status != RunStatus.FAILED.value:
                raise HTTPException(409, "Only failed tasks can be recovered")
            task.run_status = RunStatus.QUEUED.value
            task.cancel_requested_at = None
            if task.stage in {TaskStage.RETRY_EXHAUSTED.value, TaskStage.IN_PROGRESS.value}:
                task.stage = TaskStage.READY.value
            task.available_at = now
        target_stage = TaskStage(task.stage)
        recovery_transition = (
            command in {TaskCommand.RESUME, TaskCommand.RETRY}
            and before_stage == TaskStage.IN_PROGRESS.value
            and target_stage == TaskStage.READY
        )
        if command == TaskCommand.CANCEL:
            target_stage = TaskStage.CANCELLED
        if target_stage.value != before_stage:
            assert_transition(
                TaskStage(before_stage),
                target_stage,
                expected_version=expected_version,
                current_version=task.version,
                reason=reason,
                return_stage=TaskStage(task.return_stage) if task.return_stage else None,
                recovery=recovery_transition,
            )
            task.stage = target_stage.value
        version_before = task.version
        task.version += 1
        db.add(
            TaskTransition(
                task_id=task.id, project_id=task.project_id, actor_id=actor.id,
                from_stage=before_stage, to_stage=task.stage,
                from_run_status=before_status, to_run_status=task.run_status,
                reason=reason, command=command.value, command_key=command_key,
                request_hash=digest, version_before=version_before, version_after=task.version
            )
        )
        await _record_event(
            db, task=task,
            event_type="task.ready" if task.stage == TaskStage.READY.value
            and command in {TaskCommand.RESUME, TaskCommand.RETRY}
            else f"task.{command.value}",
            actor_id=actor.id,
            run_id=active_run.id if active_run else None,
            payload={"command": command.value, "reason": reason,
                     "run_status": task.run_status, "version": task.version,
                     "cancel_requested": active_run is not None and command in {TaskCommand.PAUSE, TaskCommand.CANCEL}}
        )
        await db.commit()
        return task, False
    except TaskVersionConflict as exc:
        await db.rollback()
        raise HTTPException(
            409, detail={"code": "version_conflict", "current_version": exc.current}
        ) from exc
    except (InvalidTaskTransition, TransitionGuardFailed) as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code": "transition_rejected", "reason": str(exc)}) from exc
    except Exception:
        await db.rollback()
        raise
