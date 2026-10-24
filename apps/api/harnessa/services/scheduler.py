"""PostgreSQL-backed scheduler, leases, retries, checkpoint recovery, and fencing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.execution import (
    ExecutionAdapter,
    ExecutionFailure,
    RunInput,
    StaleLease,
    TaskInput,
)
from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
)
from harnessa.adapters.verification import VerificationManifest
from harnessa.config import settings
from harnessa.contracts import RetryClass, RunStatus, TaskStage
from harnessa.db.model_gateway import ModelBudgetCounter, ModelBudgetPolicy
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import (
    DeadLetter,
    Run,
    RunArtifact,
    RunCheckpoint,
    TaskLease,
    TaskTransition,
)
from harnessa.db.review import VerificationExecution
from harnessa.db.session import SessionLocal
from harnessa.db.specification import (
    SpecificationInterview,
    SpecificationQuestion,
    SpecificationReadinessEvaluation,
)
from harnessa.services.lifecycle import _record_event, record_system_transition
from harnessa.services.model_gateway import budget_policy_digest
from harnessa.services.specifications import specification_workspace_manager
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class ClaimedRun:
    task_id: UUID
    project_id: UUID
    run_id: UUID
    owner_id: str
    fencing_token: int
    attempt: int
    max_attempts: int
    max_wall_seconds: int
    task: TaskInput
    checkpoint: Mapping[str, Any] | None = None


def retry_delay_seconds(
    attempt: int,
    retry_class: str,
    *,
    retry_after_seconds: float | None = None,
    random_value: float | None = None,
) -> float:
    if retry_class == RetryClass.RATE_LIMITED.value and retry_after_seconds is not None:
        return max(0.0, min(retry_after_seconds, 3600.0))
    random_value = random.random() if random_value is None else random_value
    base = min(300.0, 2.0 ** max(0, attempt - 1))
    return max(0.0, base * (0.8 + 0.4 * random_value))


class Scheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        *,
        global_limit: int | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: int | None = None,
        max_attempts: int | None = None,
        max_wall_seconds: int | None = None,
        rng: Callable[[], float] = random.random,
        artifact_store: LocalArtifactStore | None = None,
    ) -> None:
        self.sessions = session_factory
        self.global_limit = global_limit or settings.max_concurrent_runs
        self.lease_seconds = lease_seconds or settings.run_lease_seconds
        self.heartbeat_seconds = heartbeat_seconds or settings.run_heartbeat_seconds
        self.max_attempts = max_attempts or settings.run_max_attempts
        self.max_wall_seconds = max_wall_seconds or settings.run_max_wall_seconds
        self.rng = rng
        self.artifact_store = artifact_store or LocalArtifactStore(Path(settings.artifact_root))

    async def claim(self, owner_id: str, adapter_name: str) -> ClaimedRun | None:
        await self.recover_expired_leases()
        now = datetime.now(UTC)
        async with self.sessions() as db:
            try:
                await db.execute(text("SELECT pg_advisory_xact_lock(710203041)"))
                global_active = (
                    await db.scalar(
                        select(func.count(TaskLease.id)).where(
                            TaskLease.owner_id.is_not(None), TaskLease.expires_at > now
                        )
                    )
                    or 0
                )
                if global_active >= self.global_limit:
                    await db.rollback()
                    return None
                query = (
                    select(Task, Project)
                    .join(Project, Project.id == Task.project_id)
                    .where(
                        Task.stage == TaskStage.READY.value,
                        Task.run_status == RunStatus.QUEUED.value,
                        Task.cancel_requested_at.is_(None),
                        or_(Task.available_at.is_(None), Task.available_at <= now),
                    )
                    .order_by(
                        Project.last_dispatched_at.asc().nulls_first(),
                        Task.priority.desc(),
                        Task.created_at.asc(),
                        Task.id,
                    )
                    .with_for_update(skip_locked=True, of=Task)
                )
                candidates = (await db.execute(query.limit(100))).all()
                selected: tuple[Task, Project] | None = None
                readiness_invalidated = False
                for task, project in candidates:
                    adapter_unavailable = (
                        project.execution_adapter == "disabled"
                        or not settings.acp_execution_enabled
                    )
                    if adapter_unavailable:
                        reason = (
                            "execution_adapter_disabled"
                            if project.execution_adapter == "disabled"
                            else "execution_adapter_unavailable"
                        )
                        if task.blocked_reason != reason:
                            task.blocked_reason = reason
                            await _record_event(
                                db,
                                task=task,
                                event_type="task.dispatch_blocked",
                                actor_id=None,
                                payload={"reason": reason, "adapter": project.execution_adapter},
                            )
                            readiness_invalidated = True
                        continue
                    interview = await db.scalar(
                        select(SpecificationInterview)
                        .where(SpecificationInterview.task_id == task.id)
                        .with_for_update()
                    )
                    evaluation = None
                    repository = None
                    manifest = None
                    if interview is not None:
                        evaluation = await db.scalar(
                            select(SpecificationReadinessEvaluation)
                            .where(SpecificationReadinessEvaluation.interview_id == interview.id)
                            .order_by(SpecificationReadinessEvaluation.created_at.desc())
                            .limit(1)
                        )
                        repository = await db.get(Repository, interview.repository_id)
                        from harnessa.db.review import VerificationManifestRevision

                        manifest = await db.scalar(
                            select(VerificationManifestRevision)
                            .where(
                                VerificationManifestRevision.repository_id
                                == interview.repository_id
                            )
                            .order_by(VerificationManifestRevision.revision.desc())
                            .limit(1)
                        )
                        manifest_snapshot_valid = False
                        if manifest is not None and repository is not None:
                            try:
                                manifest_source = (
                                    await specification_workspace_manager().read_file_at_revision(
                                        repository, manifest.source_revision, manifest.source_path
                                    )
                                )
                                manifest_document = json.loads(manifest_source)
                                parsed_manifest = VerificationManifest.model_validate(
                                    {**manifest_document, "revision": manifest.revision}
                                )
                                manifest_snapshot_valid = (
                                    hashlib.sha256(manifest_source).hexdigest()
                                    == manifest.source_digest
                                    and parsed_manifest.digest == manifest.manifest_digest
                                    and manifest.source_revision == interview.base_sha
                                )
                            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                                manifest_snapshot_valid = False
                        policy = await db.scalar(
                            select(ModelBudgetPolicy).where(
                                ModelBudgetPolicy.project_id == project.id
                            )
                        )
                        project_counter = await db.scalar(
                            select(ModelBudgetCounter).where(
                                ModelBudgetCounter.project_id == project.id,
                                ModelBudgetCounter.workload == "interactive",
                                ModelBudgetCounter.scope == "project",
                                ModelBudgetCounter.scope_id == project.id,
                            )
                        )
                        task_counter = await db.scalar(
                            select(ModelBudgetCounter).where(
                                ModelBudgetCounter.project_id == project.id,
                                ModelBudgetCounter.workload == "interactive",
                                ModelBudgetCounter.task_id == task.id,
                                ModelBudgetCounter.scope == "task",
                                ModelBudgetCounter.scope_id == task.id,
                            )
                        )
                        from harnessa.contracts import (
                            ModelPoolKind,
                            ModelProtocol,
                            RouteRequirements,
                        )
                        from harnessa.services.model_gateway import has_compatible_configured_route

                        route_ready = bool(
                            policy is not None
                            and policy.max_output_tokens >= settings.acp_model_max_output_tokens
                            and await has_compatible_configured_route(
                                db,
                                organization_id=project.organization_id,
                                pool_kind=ModelPoolKind.INTERACTIVE.value,
                                alias_name=settings.acp_model_alias,
                                requirements=RouteRequirements(
                                    protocol=ModelProtocol.CHAT_COMPLETIONS,
                                    tools=True,
                                    minimum_context=min(
                                        2_000_000,
                                        settings.acp_model_max_request_bytes
                                        + settings.acp_model_max_output_tokens,
                                    ),
                                    usage_reporting=True,
                                    # Cancellation is fenced by Harnessa locally;
                                    # upstream generation may continue after disconnect.
                                    cancellation=False,
                                ),
                            )
                        )
                        budget_current = bool(
                            policy is not None
                            and evaluation is not None
                            and evaluation.gates.get("budget_policy_digest")
                            == budget_policy_digest(policy)
                            and (
                                project_counter is None
                                or (
                                    project_counter.billed_usd + project_counter.reserved_usd
                                    < policy.project_limit_usd
                                    and project_counter.used_tokens
                                    + project_counter.reserved_tokens
                                    < policy.project_token_limit
                                    and project_counter.used_steps < policy.max_steps
                                )
                            )
                            and (
                                task_counter is None
                                or (
                                    task_counter.billed_usd + task_counter.reserved_usd
                                    < policy.task_limit_usd
                                    and task_counter.used_tokens + task_counter.reserved_tokens
                                    < policy.task_token_limit
                                    and task_counter.used_steps < policy.max_steps
                                )
                            )
                        )
                        frozen_ready = bool(
                            interview.readiness_state == "READY"
                            and evaluation is not None
                            and evaluation.state == "READY"
                            and evaluation.interview_revision == interview.revision
                            and evaluation.artifact_revision == interview.artifact_revision
                            and evaluation.content_hash == interview.artifact_hash
                            and evaluation.base_sha == interview.base_sha
                            and evaluation.policy_revision == interview.policy_revision
                            and evaluation.gates.get("execution_capabilities", {}).get(
                                "model_route_ready"
                            )
                            is True
                            and route_ready
                            and budget_current
                            and manifest is not None
                            and manifest_snapshot_valid
                            and manifest.source_revision == interview.base_sha
                        )
                        try:
                            current_base = (
                                await specification_workspace_manager().current_base(repository)
                                if repository is not None
                                else None
                            )
                        except (OSError, SpecificationWorkspaceError):
                            current_base = None
                        if not frozen_ready or current_base != interview.base_sha:
                            interview.readiness_state = "STALE"
                            readiness_invalidated = True
                            await record_system_transition(
                                db,
                                task=task,
                                target=TaskStage.SPECIFYING,
                                reason="Specification readiness or repository base is not current",
                                event_type="specification.dispatch_blocked",
                                payload={
                                    "interview_id": str(interview.id),
                                    "evaluation_id": str(evaluation.id) if evaluation else None,
                                    "base_current": current_base == interview.base_sha,
                                    "frozen_ready": frozen_ready,
                                },
                            )
                            continue
                    if (
                        interview is None
                        or evaluation is None
                        or manifest is None
                        or repository is None
                    ):
                        reason = "readiness_or_manifest_required"
                        should_record = task.blocked_reason != reason
                        task.blocked_reason = reason
                        readiness_invalidated = True
                        if should_record:
                            await _record_event(
                                db,
                                task=task,
                                event_type="task.dispatch_blocked",
                                actor_id=None,
                                payload={
                                    "reason": task.blocked_reason,
                                    "requires": [
                                        "ready_specification",
                                        "current_approved_manifest",
                                    ],
                                },
                            )
                        continue
                    project_active = (
                        await db.scalar(
                            select(func.count(TaskLease.id))
                            .join(Task, Task.id == TaskLease.task_id)
                            .where(
                                Task.project_id == project.id,
                                TaskLease.owner_id.is_not(None),
                                TaskLease.expires_at > now,
                            )
                        )
                        or 0
                    )
                    lease = await db.scalar(
                        select(TaskLease).where(TaskLease.task_id == task.id).with_for_update()
                    )
                    if project_active >= project.max_concurrency:
                        continue
                    if lease and lease.owner_id and lease.expires_at and lease.expires_at > now:
                        continue
                    selected = (task, project)
                    break
                if selected is None:
                    if readiness_invalidated:
                        await db.commit()
                    else:
                        await db.rollback()
                    return None

                task, project = selected
                if interview is None or manifest is None:
                    raise RuntimeError(
                        "Dispatch readiness fence lost its approved specification or manifest"
                    )
                assert repository is not None
                if policy is None:
                    raise RuntimeError("READY dispatch lost its current model budget policy")
                attempt_limit = min(self.max_attempts, policy.max_attempts)
                wall_limit = min(self.max_wall_seconds, policy.max_wall_seconds)
                specification_manager = specification_workspace_manager()
                approved_workspace = await specification_manager.open(
                    interview.id,
                    repository,
                    change_id=interview.change_id,
                    base_sha=interview.base_sha,
                    head_sha=interview.head_sha,
                )
                specification_files = await specification_manager.read_bundle(
                    approved_workspace, interview.head_sha
                )
                if sum(len(value.encode()) for value in specification_files.values()) > 512_000:
                    raise RuntimeError("Approved specification exceeds the dispatch context limit")
                try:
                    harness_instructions = (
                        await specification_manager.read_file_at_revision(
                            repository, interview.base_sha, "AGENTS.md", limit_bytes=128_000
                        )
                    ).decode("utf-8")
                except SpecificationWorkspaceError:
                    harness_instructions = ""
                harness_digest = hashlib.sha256(harness_instructions.encode("utf-8")).hexdigest()
                if task.blocked_reason == "verification_repair":
                    from harnessa.db.review import ReviewBundle

                    stale_bundles = await db.scalars(
                        select(ReviewBundle)
                        .where(
                            ReviewBundle.task_id == task.id,
                            ReviewBundle.status.in_(["prepared", "published"]),
                        )
                        .with_for_update()
                    )
                    for stale_bundle in stale_bundles:
                        stale_bundle.status = "invalidated"
                        stale_bundle.invalidated_reason = (
                            "A new repair run is changing the reviewed source revision"
                        )
                verification_feedback: Mapping[str, Any] | None = None
                if task.blocked_reason == "verification_repair":
                    prior = await db.scalar(
                        select(VerificationExecution)
                        .where(VerificationExecution.task_id == task.id)
                        .order_by(VerificationExecution.created_at.desc())
                        .limit(1)
                    )
                    if prior is None:
                        raise RuntimeError("Verification repair was queued without prior evidence")
                    verification_feedback = _repair_feedback(prior.result)
                lease = await db.scalar(
                    select(TaskLease).where(TaskLease.task_id == task.id).with_for_update()
                )
                token = (lease.fencing_token + 1) if lease else 1
                if lease is None:
                    lease = TaskLease(task_id=task.id, fencing_token=0)
                    db.add(lease)
                previous_runs = await db.execute(
                    select(Run).where(Run.task_id == task.id).order_by(Run.attempt.desc())
                )
                previous = previous_runs.scalars().first()
                attempt = (previous.attempt + 1) if previous else 1
                if attempt > attempt_limit:
                    task.blocked_reason = "attempt_limit_exhausted"
                    await record_system_transition(
                        db,
                        task=task,
                        target=TaskStage.RETRY_EXHAUSTED,
                        reason="Attempt limit exhausted before dispatch",
                        event_type="task.retry_exhausted",
                        run_status=RunStatus.FAILED,
                        payload={"attempt": attempt, "limit": attempt_limit},
                    )
                    db.add(
                        DeadLetter(
                            project_id=project.id,
                            task_id=task.id,
                            run_id=previous.id if previous else None,
                            kind="attempt_limit_exhausted",
                            reason="Run attempt limit reached before dispatch",
                            payload={"attempt": attempt, "limit": attempt_limit},
                        )
                    )
                    await db.commit()
                    return None

                first_started = await db.scalar(
                    select(func.min(Run.started_at)).where(Run.task_id == task.id)
                )
                if first_started and (now - first_started).total_seconds() >= wall_limit:
                    task.blocked_reason = "wall_time_limit_exhausted"
                    await record_system_transition(
                        db,
                        task=task,
                        target=TaskStage.RETRY_EXHAUSTED,
                        reason="Wall time limit exhausted before dispatch",
                        event_type="task.retry_exhausted",
                        run_status=RunStatus.FAILED,
                        payload={"limit_seconds": wall_limit},
                    )
                    db.add(
                        DeadLetter(
                            project_id=project.id,
                            task_id=task.id,
                            run_id=previous.id if previous else None,
                            kind="wall_time_limit_exhausted",
                            reason="Run wall-time limit reached before dispatch",
                            payload={"limit_seconds": wall_limit},
                        )
                    )
                    await db.commit()
                    return None

                checkpoint = None
                if task.blocked_reason == "worker_lost_checkpoint":
                    row = await db.scalar(
                        select(RunCheckpoint)
                        .where(RunCheckpoint.task_id == task.id, RunCheckpoint.resumable.is_(True))
                        .order_by(RunCheckpoint.created_at.desc())
                        .limit(1)
                    )
                    if row:
                        checkpoint = row.state
                run = Run(
                    task_id=task.id,
                    project_id=project.id,
                    attempt=attempt,
                    status=RunStatus.RUNNING.value,
                    fencing_token=token,
                    started_at=now,
                    heartbeat_at=now,
                    deadline_at=now + timedelta(seconds=wall_limit),
                    adapter_name=adapter_name,
                    checkpoint_id=row.id if checkpoint and row else None,
                    resume_from_run_id=previous.id if checkpoint and previous else None,
                    base_sha=interview.base_sha,
                    specification_hash=interview.artifact_hash,
                    specification_revision=interview.artifact_revision,
                    specification_commit_sha=interview.head_sha,
                    policy_revision=interview.policy_revision,
                    budget_policy_digest=budget_policy_digest(policy),
                    harness_revision=settings.acp_harness_revision,
                    harness_digest=harness_digest,
                    agent_image=settings.acp_agent_image,
                    verification_manifest_revision_id=manifest.id,
                )
                db.add(run)
                await db.flush()
                lease.run_id = run.id
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.heartbeat_at = now
                lease.expires_at = now + timedelta(seconds=self.lease_seconds)
                project.last_dispatched_at = now
                task.blocked_reason = None
                await record_system_transition(
                    db,
                    task=task,
                    target=TaskStage.IN_PROGRESS,
                    reason="Scheduler atomically claimed a READY task",
                    event_type="task.run_claimed",
                    payload={"run_id": str(run.id), "attempt": attempt, "fencing_token": token},
                    run_id=run.id,
                    run_status=RunStatus.RUNNING,
                )
                await db.commit()
                return ClaimedRun(
                    task_id=task.id,
                    project_id=project.id,
                    run_id=run.id,
                    owner_id=owner_id,
                    fencing_token=token,
                    attempt=attempt,
                    max_attempts=attempt_limit,
                    max_wall_seconds=wall_limit,
                    task=TaskInput(
                        task.id,
                        project.id,
                        task.title,
                        task.description,
                        verification_feedback=verification_feedback,
                        approved_base_sha=interview.base_sha,
                        specification_files=specification_files,
                        specification_hash=interview.artifact_hash,
                        specification_revision=interview.artifact_revision,
                        harness_instructions=harness_instructions,
                        harness_revision=settings.acp_harness_revision,
                        harness_digest=harness_digest,
                    ),
                    checkpoint=checkpoint,
                )
            except Exception:
                await db.rollback()
                raise

    async def heartbeat(self, job: ClaimedRun) -> bool:
        now = datetime.now(UTC)
        async with self.sessions() as db:
            result = await db.execute(
                update(TaskLease)
                .where(
                    TaskLease.task_id == job.task_id,
                    TaskLease.run_id == job.run_id,
                    TaskLease.owner_id == job.owner_id,
                    TaskLease.fencing_token == job.fencing_token,
                    TaskLease.expires_at > now,
                )
                .values(heartbeat_at=now, expires_at=now + timedelta(seconds=self.lease_seconds))
            )
            if cast(Any, result).rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                update(Run)
                .where(
                    Run.id == job.run_id,
                    Run.fencing_token == job.fencing_token,
                    Run.status == RunStatus.RUNNING.value,
                )
                .values(heartbeat_at=now)
            )
            await db.commit()
            return True

    async def save_checkpoint(self, job: ClaimedRun, state: Mapping[str, Any]) -> UUID:
        async with self.sessions() as db:
            try:
                lease = await db.scalar(
                    select(TaskLease)
                    .where(
                        TaskLease.task_id == job.task_id,
                        TaskLease.run_id == job.run_id,
                        TaskLease.owner_id == job.owner_id,
                        TaskLease.fencing_token == job.fencing_token,
                        TaskLease.expires_at > func.now(),
                    )
                    .with_for_update()
                )
                task = await db.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
                run = await db.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
                if (
                    lease is None
                    or task is None
                    or run is None
                    or run.task_id != task.id
                    or run.project_id != job.project_id
                    or run.fencing_token != job.fencing_token
                    or run.status != RunStatus.RUNNING.value
                    or run.cancel_requested_at is not None
                    or task.run_status != RunStatus.RUNNING.value
                    or task.cancel_requested_at is not None
                    or task.stage != TaskStage.IN_PROGRESS.value
                ):
                    raise StaleLease("Checkpoint rejected because the lease is no longer current")
                sequence = await db.scalar(
                    select(func.coalesce(func.max(RunCheckpoint.sequence), 0)).where(
                        RunCheckpoint.run_id == job.run_id
                    )
                )
                import hashlib
                import json

                data = dict(state)
                digest = hashlib.sha256(
                    json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                checkpoint = RunCheckpoint(
                    run_id=job.run_id,
                    task_id=job.task_id,
                    project_id=job.project_id,
                    sequence=(sequence or 0) + 1,
                    fencing_token=job.fencing_token,
                    state=data,
                    state_hash=digest,
                    resumable=bool(state.get("resumable", True)),
                )
                db.add(checkpoint)
                await db.flush()
                run.checkpoint_id = checkpoint.id
                if state.get("phase") == "session_update":
                    counts = state.get("update_counts", {})
                    safe_counts = (
                        {
                            str(key)[:80]: int(value)
                            for key, value in counts.items()
                            if isinstance(key, str) and isinstance(value, int) and value >= 0
                        }
                        if isinstance(counts, Mapping)
                        else {}
                    )
                    await _record_event(
                        db,
                        task=task,
                        event_type="run.progress",
                        actor_id=None,
                        run_id=run.id,
                        payload={"phase": "agent_updates", "update_counts": safe_counts},
                    )
                await db.commit()
                return checkpoint.id
            except Exception:
                await db.rollback()
                raise

    async def publish_artifact(
        self,
        job: ClaimedRun,
        content: bytes,
        media_type: str,
    ) -> Mapping[str, Any]:
        """Persist a CAS blob and scoped publication record under the current fence."""
        if len(content) > settings.acp_artifact_limit_bytes:
            raise ValueError("Run artifact exceeded its configured byte limit")
        if not media_type or len(media_type) > 160:
            raise ValueError("Run artifact media type is invalid")
        async with self.sessions() as db:
            try:
                lease = await db.scalar(
                    select(TaskLease)
                    .where(
                        TaskLease.task_id == job.task_id,
                        TaskLease.run_id == job.run_id,
                        TaskLease.owner_id == job.owner_id,
                        TaskLease.fencing_token == job.fencing_token,
                        TaskLease.expires_at > func.now(),
                    )
                    .with_for_update()
                )
                task = await db.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
                run = await db.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
                project = await db.scalar(
                    select(Project).where(Project.id == job.project_id).with_for_update()
                )
                if (
                    lease is None
                    or task is None
                    or run is None
                    or project is None
                    or task.project_id != project.id
                    or run.task_id != task.id
                    or run.project_id != project.id
                    or run.fencing_token != job.fencing_token
                    or run.status != RunStatus.RUNNING.value
                    or run.cancel_requested_at is not None
                    or task.run_status != RunStatus.RUNNING.value
                    or task.cancel_requested_at is not None
                    or task.stage != TaskStage.IN_PROGRESS.value
                ):
                    raise StaleLease("Artifact rejected because the lease is no longer current")

                stored = await asyncio.to_thread(
                    self.artifact_store.put_bytes, content, media_type=media_type
                )
                # The write may take time. Recheck the expiry against database time before
                # creating the durable reference; a crash here leaves only a GC-safe orphan.
                current_lease = await db.scalar(
                    select(TaskLease).where(
                        TaskLease.task_id == job.task_id,
                        TaskLease.run_id == job.run_id,
                        TaskLease.owner_id == job.owner_id,
                        TaskLease.fencing_token == job.fencing_token,
                        TaskLease.expires_at > func.now(),
                    )
                )
                if current_lease is None:
                    raise StaleLease("Artifact rejected because the lease expired during storage")

                previous = await db.scalar(
                    select(RunArtifact).where(
                        RunArtifact.run_id == job.run_id,
                        RunArtifact.fencing_token == job.fencing_token,
                        RunArtifact.digest == stored.digest,
                    )
                )
                if previous is not None:
                    if previous.media_type != stored.media_type:
                        raise ValueError(
                            "Artifact digest was already published with another media type"
                        )
                    await db.commit()
                    return _artifact_summary(previous)

                artifact = RunArtifact(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    task_id=task.id,
                    run_id=run.id,
                    fencing_token=job.fencing_token,
                    digest=stored.digest,
                    media_type=stored.media_type,
                    size_bytes=stored.size_bytes,
                    storage_key=stored.storage_key,
                )
                db.add(artifact)
                await db.flush()
                await _record_event(
                    db,
                    task=task,
                    event_type="run.artifact_published",
                    actor_id=None,
                    run_id=run.id,
                    payload={
                        "artifact_id": str(artifact.id),
                        "digest": artifact.digest,
                        "media_type": artifact.media_type,
                        "size_bytes": artifact.size_bytes,
                    },
                )
                await db.commit()
                return _artifact_summary(artifact)
            except Exception:
                await db.rollback()
                raise

    async def is_current_fence(self, run_id: UUID, fencing_token: int) -> bool:
        async with self.sessions() as db:
            current = await db.scalar(
                select(Run.id)
                .join(TaskLease, TaskLease.run_id == Run.id)
                .join(Task, Task.id == Run.task_id)
                .where(
                    Run.id == run_id,
                    Run.fencing_token == fencing_token,
                    Run.status == RunStatus.RUNNING.value,
                    Run.cancel_requested_at.is_(None),
                    Task.run_status == RunStatus.RUNNING.value,
                    Task.stage == TaskStage.IN_PROGRESS.value,
                    Task.cancel_requested_at.is_(None),
                    TaskLease.owner_id.is_not(None),
                    TaskLease.task_id == Run.task_id,
                    TaskLease.expires_at > func.now(),
                    TaskLease.fencing_token == fencing_token,
                )
            )
            return current is not None

    async def reconcile_orphan_artifacts(self, *, minimum_age_seconds: int = 3600) -> int:
        async with self.sessions() as db:
            keys = set((await db.scalars(select(RunArtifact.storage_key))).all())
        return await asyncio.to_thread(
            self.artifact_store.remove_unreferenced,
            keys,
            minimum_age_seconds=minimum_age_seconds,
        )

    async def is_cancelled(self, job: ClaimedRun) -> bool:
        async with self.sessions() as db:
            task = await db.get(Task, job.task_id)
            run = await db.get(Run, job.run_id)
            return bool(
                task is None
                or run is None
                or task.run_status in {RunStatus.CANCELLED.value, RunStatus.PAUSED.value}
                or task.cancel_requested_at
                or run.cancel_requested_at
            )

    async def complete(self, job: ClaimedRun, result: Mapping[str, Any]) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as db:
            try:
                lease = await db.scalar(
                    select(TaskLease)
                    .where(
                        TaskLease.task_id == job.task_id,
                        TaskLease.run_id == job.run_id,
                        TaskLease.owner_id == job.owner_id,
                        TaskLease.fencing_token == job.fencing_token,
                        TaskLease.expires_at > now,
                    )
                    .with_for_update()
                )
                task = await db.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
                run = await db.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
                if not lease or not task or not run or run.fencing_token != job.fencing_token:
                    raise StaleLease("Completion rejected because the lease is no longer current")
                if task.cancel_requested_at or task.run_status in {
                    RunStatus.CANCELLED.value,
                    RunStatus.PAUSED.value,
                }:
                    await self._finish_interrupted(db, task, run, lease, now)
                    await db.commit()
                    return
                run.status = RunStatus.SUCCEEDED.value
                run.finished_at = now
                run.error_code = None
                run.error_message = None
                run.execution_result = dict(result)
                result_revision = result.get("source_revision")
                if (
                    isinstance(result_revision, str)
                    and len(result_revision) in {40, 64}
                    and all(character in "0123456789abcdef" for character in result_revision)
                ):
                    if run.source_revision not in {None, result_revision}:
                        raise StaleLease("Execution result revision changed after it was bound")
                    run.source_revision = result_revision
                task.stage = TaskStage.VERIFYING.value
                task.run_status = RunStatus.SUCCEEDED.value
                task.version += 1
                lease.owner_id = None
                lease.expires_at = now
                lease.heartbeat_at = now
                db.add(
                    TaskTransition(
                        task_id=task.id,
                        project_id=task.project_id,
                        actor_id=None,
                        from_stage=TaskStage.IN_PROGRESS.value,
                        to_stage=TaskStage.VERIFYING.value,
                        from_run_status=RunStatus.RUNNING.value,
                        to_run_status=RunStatus.SUCCEEDED.value,
                        reason="Implementation completed; independent verification is pending",
                        command="implementation_succeeded",
                        version_before=task.version - 1,
                        version_after=task.version,
                    )
                )
                await _record_event(
                    db,
                    task=task,
                    event_type="task.verification_started",
                    actor_id=None,
                    run_id=run.id,
                    payload={
                        "result": dict(result),
                        "source_revision": run.source_revision,
                        "version": task.version,
                    },
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def fail(self, job: ClaimedRun, failure: ExecutionFailure) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as db:
            try:
                lease = await db.scalar(
                    select(TaskLease)
                    .where(
                        TaskLease.task_id == job.task_id,
                        TaskLease.run_id == job.run_id,
                        TaskLease.owner_id == job.owner_id,
                        TaskLease.fencing_token == job.fencing_token,
                        TaskLease.expires_at > now,
                    )
                    .with_for_update()
                )
                task = await db.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
                run = await db.scalar(select(Run).where(Run.id == job.run_id).with_for_update())
                if not lease or not task or not run or run.fencing_token != job.fencing_token:
                    raise StaleLease(
                        "Failure write rejected because the lease is no longer current"
                    )
                run.status = RunStatus.FAILED.value
                run.retry_class = failure.retry_class
                run.error_code = failure.code
                run.error_message = str(failure)
                run.finished_at = now
                lease.owner_id = None
                lease.expires_at = now
                attempt_exhausted = job.attempt >= job.max_attempts
                first_started = await db.scalar(
                    select(func.min(Run.started_at)).where(Run.task_id == task.id)
                )
                wall_exhausted = bool(
                    first_started and (now - first_started).total_seconds() >= job.max_wall_seconds
                )
                retryable = failure.retry_class in {
                    RetryClass.TRANSIENT.value,
                    RetryClass.RATE_LIMITED.value,
                }
                if not retryable or attempt_exhausted or wall_exhausted:
                    task.blocked_reason = (
                        "attempt_limit_exhausted"
                        if attempt_exhausted
                        else "wall_time_limit_exhausted"
                        if wall_exhausted
                        else failure.code
                    )
                    if retryable:
                        await record_system_transition(
                            db,
                            task=task,
                            target=TaskStage.RETRY_EXHAUSTED,
                            reason=f"Retry policy stopped execution: {failure.code}",
                            event_type="task.retry_exhausted",
                            run_id=run.id,
                            run_status=RunStatus.FAILED,
                            payload={"retry_class": failure.retry_class, "attempt": job.attempt},
                        )
                    else:
                        task.run_status = RunStatus.FAILED.value
                        task.version += 1
                        db.add(
                            TaskTransition(
                                task_id=task.id,
                                project_id=task.project_id,
                                actor_id=None,
                                from_stage=task.stage,
                                to_stage=task.stage,
                                from_run_status=RunStatus.RUNNING.value,
                                to_run_status=RunStatus.FAILED.value,
                                reason=str(failure),
                                command="run_failed",
                                version_before=task.version - 1,
                                version_after=task.version,
                            )
                        )
                        await _record_event(
                            db,
                            task=task,
                            event_type="run.failed",
                            actor_id=None,
                            run_id=run.id,
                            payload={
                                "code": failure.code,
                                "retry_class": failure.retry_class,
                                "version": task.version,
                            },
                        )
                    db.add(
                        DeadLetter(
                            project_id=task.project_id,
                            task_id=task.id,
                            run_id=run.id,
                            kind=failure.code,
                            reason=str(failure),
                            payload={"retry_class": failure.retry_class, "attempt": job.attempt},
                        )
                    )
                else:
                    delay = retry_delay_seconds(
                        job.attempt,
                        failure.retry_class,
                        retry_after_seconds=failure.retry_after_seconds,
                        random_value=self.rng(),
                    )
                    task.available_at = now + timedelta(seconds=delay)
                    task.blocked_reason = "retry_backoff"
                    await record_system_transition(
                        db,
                        task=task,
                        target=TaskStage.READY,
                        reason=f"Retrying {failure.retry_class} failure after bounded backoff",
                        event_type="task.ready",
                        run_id=run.id,
                        run_status=RunStatus.QUEUED,
                        recovery=True,
                        payload={
                            "retry_class": failure.retry_class,
                            "attempt": job.attempt,
                            "delay_seconds": delay,
                        },
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _finish_interrupted(
        self, db: AsyncSession, task: Task, run: Run, lease: TaskLease, now: datetime
    ) -> None:
        cancelled = task.run_status == RunStatus.CANCELLED.value or task.cancel_requested_at
        run.status = RunStatus.CANCELLED.value if cancelled else RunStatus.PAUSED.value
        run.finished_at = now
        lease.owner_id = None
        lease.expires_at = now
        task.version += 1
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=task.project_id,
                actor_id=None,
                from_stage=task.stage,
                to_stage=task.stage,
                from_run_status=RunStatus.RUNNING.value,
                to_run_status=run.status,
                reason="Execution acknowledged cancellation" if cancelled else "Execution paused",
                command="run_cancelled" if cancelled else "run_paused",
                version_before=task.version - 1,
                version_after=task.version,
            )
        )
        await _record_event(
            db,
            task=task,
            event_type="run.cancelled" if cancelled else "run.paused",
            actor_id=None,
            run_id=run.id,
            payload={"version": task.version, "run_status": run.status},
        )

    async def recover_expired_leases(self) -> int:
        now = datetime.now(UTC)
        recovered = 0
        async with self.sessions() as db:
            try:
                leases = (
                    (
                        await db.execute(
                            select(TaskLease)
                            .where(TaskLease.owner_id.is_not(None), TaskLease.expires_at <= now)
                            .order_by(TaskLease.expires_at)
                            .with_for_update(skip_locked=True)
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )
                for lease in leases:
                    task = await db.scalar(
                        select(Task).where(Task.id == lease.task_id).with_for_update()
                    )
                    run = await db.get(Run, lease.run_id) if lease.run_id else None
                    lease.owner_id = None
                    lease.expires_at = now
                    if not task or not run or run.status != RunStatus.RUNNING.value:
                        continue
                    if task.run_status in {RunStatus.CANCELLED.value, RunStatus.PAUSED.value}:
                        run.status = task.run_status
                        run.finished_at = now
                        recovered += 1
                        continue
                    if (
                        task.stage == TaskStage.CLARIFYING.value
                        and task.run_status == RunStatus.WAITING_FOR_INPUT.value
                    ):
                        question_id = await db.scalar(
                            select(SpecificationQuestion.id)
                            .where(
                                SpecificationQuestion.run_id == run.id,
                                SpecificationQuestion.status == "open",
                                SpecificationQuestion.fencing_token == lease.fencing_token,
                            )
                            .order_by(SpecificationQuestion.created_at.desc())
                            .limit(1)
                        )
                        run.status = RunStatus.FAILED.value
                        run.error_code = "worker_lost_waiting_for_input"
                        run.error_message = (
                            "Worker lease expired while a durable clarification was awaiting input"
                        )
                        run.retry_class = RetryClass.TRANSIENT.value
                        run.finished_at = now
                        task.version += 1
                        db.add(
                            TaskTransition(
                                task_id=task.id,
                                project_id=task.project_id,
                                actor_id=None,
                                from_stage=TaskStage.CLARIFYING.value,
                                to_stage=TaskStage.CLARIFYING.value,
                                from_run_status=RunStatus.WAITING_FOR_INPUT.value,
                                to_run_status=RunStatus.WAITING_FOR_INPUT.value,
                                reason="Expired worker detached; durable clarification remains open",
                                command="clarification_wait_recovered",
                                version_before=task.version - 1,
                                version_after=task.version,
                            )
                        )
                        await _record_event(
                            db,
                            task=task,
                            event_type="task.clarification_wait_recovered",
                            actor_id=None,
                            run_id=run.id,
                            payload={
                                "question_id": str(question_id) if question_id else None,
                                "attempt": run.attempt,
                                "fencing_token": run.fencing_token,
                                "version": task.version,
                            },
                        )
                        recovered += 1
                        continue
                    checkpoint = await db.scalar(
                        select(RunCheckpoint)
                        .where(RunCheckpoint.run_id == run.id, RunCheckpoint.resumable.is_(True))
                        .order_by(RunCheckpoint.sequence.desc())
                        .limit(1)
                    )
                    recovery_policy = await db.scalar(
                        select(ModelBudgetPolicy).where(
                            ModelBudgetPolicy.project_id == task.project_id
                        )
                    )
                    attempt_limit = min(
                        self.max_attempts,
                        recovery_policy.max_attempts if recovery_policy else self.max_attempts,
                    )
                    wall_limit = min(
                        self.max_wall_seconds,
                        recovery_policy.max_wall_seconds
                        if recovery_policy
                        else self.max_wall_seconds,
                    )
                    first_started = await db.scalar(
                        select(func.min(Run.started_at)).where(Run.task_id == task.id)
                    )
                    wall_exhausted = bool(
                        first_started and (now - first_started).total_seconds() >= wall_limit
                    )
                    run.status = RunStatus.FAILED.value
                    run.error_code = "worker_lost"
                    run.error_message = "Worker lease expired before run completion"
                    run.retry_class = (
                        RetryClass.TRANSIENT.value if checkpoint else RetryClass.PERMANENT.value
                    )
                    run.finished_at = now
                    task.version += 1
                    if checkpoint and run.attempt < attempt_limit and not wall_exhausted:
                        task.stage = TaskStage.READY.value
                        task.run_status = RunStatus.QUEUED.value
                        task.blocked_reason = "worker_lost_checkpoint"
                        task.available_at = now
                        db.add(
                            TaskTransition(
                                task_id=task.id,
                                project_id=task.project_id,
                                actor_id=None,
                                from_stage=TaskStage.IN_PROGRESS.value,
                                to_stage=TaskStage.READY.value,
                                from_run_status=RunStatus.RUNNING.value,
                                to_run_status=RunStatus.QUEUED.value,
                                reason="Expired worker recovered from durable checkpoint",
                                command="checkpoint_recovery",
                                version_before=task.version - 1,
                                version_after=task.version,
                            )
                        )
                        await _record_event(
                            db,
                            task=task,
                            event_type="task.ready",
                            actor_id=None,
                            run_id=run.id,
                            payload={
                                "reason": "checkpoint_recovery",
                                "checkpoint_id": str(checkpoint.id),
                                "version": task.version,
                            },
                        )
                    else:
                        task.run_status = RunStatus.FAILED.value
                        task.blocked_reason = (
                            "wall_time_limit_exhausted"
                            if wall_exhausted
                            else "attempt_limit_exhausted"
                            if run.attempt >= attempt_limit
                            else "worker_lost_unrecoverable"
                        )
                        db.add(
                            TaskTransition(
                                task_id=task.id,
                                project_id=task.project_id,
                                actor_id=None,
                                from_stage=task.stage,
                                to_stage=task.stage,
                                from_run_status=RunStatus.RUNNING.value,
                                to_run_status=RunStatus.FAILED.value,
                                reason="Expired worker recovery stopped because its checkpoint or shared limits did not permit replay",
                                command="worker_lost",
                                version_before=task.version - 1,
                                version_after=task.version,
                            )
                        )
                        db.add(
                            DeadLetter(
                                project_id=task.project_id,
                                task_id=task.id,
                                run_id=run.id,
                                kind="worker_lost_unrecoverable",
                                reason="Worker lease expired and no resumable checkpoint was retained",
                                payload={"attempt": run.attempt},
                            )
                        )
                        await _record_event(
                            db,
                            task=task,
                            event_type="task.worker_lost",
                            actor_id=None,
                            run_id=run.id,
                            payload={"reason": task.blocked_reason, "version": task.version},
                        )
                    recovered += 1
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return recovered

    async def run_once(self, adapter: ExecutionAdapter, owner_id: str) -> bool:
        await self.recover_expired_leases()
        if not adapter.available:
            return False
        job = await self.claim(owner_id, adapter.name)
        if job is None:
            return False
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job, adapter))

        async def save_checkpoint(state: Mapping[str, Any]) -> None:
            await self.save_checkpoint(job, state)

        async def cancelled() -> bool:
            result = await self.is_cancelled(job)
            if result:
                await adapter.cancel(job.run_id)
            return result

        try:
            result = await adapter.execute(
                job.task,
                RunInput(job.run_id, job.attempt, job.fencing_token),
                checkpoint=job.checkpoint,
                save_checkpoint=save_checkpoint,
                is_cancelled=cancelled,
                is_current_fence=self.is_current_fence,
                publish_artifact=lambda content, media_type: self.publish_artifact(
                    job, content, media_type
                ),
            )
            if await self.is_cancelled(job):
                await self.complete(job, result)
            else:
                await self.complete(job, result)
        except asyncio.CancelledError:
            await adapter.cancel(job.run_id)
            raise
        except ExecutionFailure as failure:
            await self.fail(job, failure)
        except StaleLease:
            # The new lease owner is authoritative; stale worker output is discarded.
            return True
        except Exception as exc:  # noqa: BLE001 - adapters are isolated behind this failure boundary.
            await self.fail(
                job,
                ExecutionFailure(RetryClass.TRANSIENT.value, "adapter_error", str(exc)),
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        return True

    async def _heartbeat_loop(self, job: ClaimedRun, adapter: ExecutionAdapter) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.heartbeat(job):
                await adapter.cancel(job.run_id)
                return
            if await self.is_cancelled(job):
                await adapter.cancel(job.run_id)
                return


def _artifact_summary(artifact: RunArtifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "digest": artifact.digest,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "storage_key": artifact.storage_key,
        "run_id": str(artifact.run_id),
        "fencing_token": artifact.fencing_token,
    }


def _repair_feedback(result: Mapping[str, Any]) -> dict[str, Any]:
    """Select bounded, explicitly untrusted verifier facts for the next implementation turn."""
    criteria = result.get("criteria", [])
    commands = result.get("commands", [])
    selected_criteria = (
        [
            {
                "criterion_id": item.get("criterion_id"),
                "outcome": item.get("outcome"),
                "summary": str(item.get("summary", ""))[:500],
            }
            for item in criteria[:100]
            if isinstance(item, dict) and item.get("outcome") in {"failed", "unverified"}
        ]
        if isinstance(criteria, list)
        else []
    )
    selected_commands = (
        [
            {
                "command_id": item.get("command_id"),
                "exit_code": item.get("exit_code"),
                "stderr": str(item.get("stderr", ""))[:1200],
                "stdout": str(item.get("stdout", ""))[:1200],
            }
            for item in commands[:50]
            if isinstance(item, dict) and item.get("outcome") in {"failed", "unverified"}
        ][:8]
        if isinstance(commands, list)
        else []
    )
    return {
        "outcome": result.get("outcome", "unverified"),
        "criteria": selected_criteria,
        "commands": selected_commands,
        "evidence_is_untrusted": True,
    }
