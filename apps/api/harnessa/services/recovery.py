"""Operator recovery actions with the same audit and outbox transaction as state changes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from harnessa.contracts import RunStatus, TaskStage
from harnessa.db.models import Task, User
from harnessa.db.orchestration import DeadLetter, TaskTransition
from harnessa.domain.lifecycle import assert_transition
from harnessa.services.authorization import require_membership
from harnessa.services.lifecycle import _deduplicated_command, _lock_command_key, _record_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def replay_dead_letter(
    db: AsyncSession,
    *,
    dead_letter_id: UUID,
    actor: User,
    reason: str,
    command_key: str,
) -> tuple[Task, bool]:
    if not reason.strip() or len(reason) > 1000:
        raise HTTPException(422, "A bounded recovery reason is required")
    if not command_key or len(command_key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    digest = hashlib.sha256(
        json.dumps(
            {"dead_letter_id": str(dead_letter_id), "reason": reason},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        await _lock_command_key(db, actor.id, command_key)
        replay = await _deduplicated_command(
            db, actor_id=actor.id, command_key=command_key, request_hash=digest
        )
        if replay:
            await require_membership(db, actor, replay.project_id)
            return replay, True
        dead = await db.scalar(
            select(DeadLetter).where(DeadLetter.id == dead_letter_id).with_for_update()
        )
        if dead is None:
            raise HTTPException(404, "Dead letter not found")
        membership = await require_membership(db, actor, dead.project_id)
        if membership.role not in {"administrator", "operator"}:
            raise HTTPException(403, "Only project operators can replay dead letters")
        if dead.status != "open":
            raise HTTPException(409, "Dead letter is not open")
        task = await db.scalar(select(Task).where(Task.id == dead.task_id).with_for_update())
        if task is None:
            raise HTTPException(404, "Dead-letter task not found")
        if task.run_status != RunStatus.FAILED.value:
            raise HTTPException(409, "Only failed tasks can be recovered")
        current = TaskStage(task.stage)
        assert_transition(
            current,
            TaskStage.READY,
            expected_version=task.version,
            current_version=task.version,
            reason=f"Operator replay: {reason}",
            return_stage=TaskStage(task.return_stage) if task.return_stage else None,
            recovery=current == TaskStage.IN_PROGRESS,
        )
        version_before = task.version
        task.stage = TaskStage.READY.value
        task.run_status = RunStatus.QUEUED.value
        task.blocked_reason = "operator_replay"
        task.available_at = datetime.now(UTC)
        task.cancel_requested_at = None
        task.version += 1
        dead.status = "replayed"
        dead.replayed_by = actor.id
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=task.project_id,
                actor_id=actor.id,
                from_stage=current.value,
                to_stage=TaskStage.READY.value,
                from_run_status=RunStatus.FAILED.value,
                to_run_status=RunStatus.QUEUED.value,
                reason=reason,
                command="dead_letter_replay",
                command_key=command_key,
                request_hash=digest,
                version_before=version_before,
                version_after=task.version,
            )
        )
        await _record_event(
            db,
            task=task,
            event_type="task.ready",
            actor_id=actor.id,
            run_id=dead.run_id,
            payload={"reason": "operator_replay", "detail": reason, "version": task.version},
        )
        await db.commit()
        return task, False
    except Exception:
        await db.rollback()
        raise
