"""Durable verifier dispatch and immutable implementation review bundle creation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from harnessa.adapters.artifacts import LocalArtifactStore, StoredArtifact
from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
)
from harnessa.adapters.verification import (
    PodmanCommandExecutor,
    VerificationManifest,
    VerificationRunner,
    VerificationUnavailable,
)
from harnessa.config import settings
from harnessa.contracts import TaskStage
from harnessa.db.harness import HarnessExperiment, HarnessPolicy, HarnessProbeRun
from harnessa.db.model_gateway import ModelBudgetPolicy, ModelRequestLedger, ModelReservation
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import Run, RunArtifact, TaskLease, TaskTransition
from harnessa.db.review import (
    ReviewBundle,
    VerificationExecution,
    VerificationManifestRevision,
)
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.services.lifecycle import _record_event
from harnessa.services.specifications import specification_workspace_manager
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _harness_learning_hook(
    experiment: HarnessExperiment | None,
    probes: list[HarnessProbeRun],
    *,
    base_sha: str | None,
    specification_hash: str | None,
    specification_revision: int | None,
    policy_revision: int | None,
) -> dict[str, Any]:
    """Bind post-merge experience to a complete, successful, immutable probe set."""
    empty = {"reserved_for": "M12", "triggered": False, "probe_outcome": None}
    if experiment is None or experiment.status != "succeeded":
        return {**empty, "reason": "no_successful_harness_experiment"}
    if (
        experiment.base_sha != base_sha
        or experiment.specification_hash != specification_hash
        or experiment.specification_revision != specification_revision
        or experiment.policy_revision != policy_revision
    ):
        return {
            **empty,
            "experiment_id": str(experiment.id),
            "reason": "frozen_experiment_identity_mismatch",
        }
    phases = {probe.phase for probe in probes if probe.status == "succeeded"}
    if not {"baseline", "candidate", "composed"}.issubset(phases):
        return {
            **empty,
            "experiment_id": str(experiment.id),
            "reason": "matched_probe_set_incomplete",
        }
    candidate = experiment.result.get("experience_candidate")
    if (
        not isinstance(candidate, dict)
        or not isinstance(candidate.get("cue"), str)
        or not isinstance(candidate.get("lesson"), str)
        or len(candidate["cue"]) > 1000
        or len(candidate["lesson"]) > 2000
    ):
        return {
            **empty,
            "experiment_id": str(experiment.id),
            "reason": "no_evidence_backed_experience_candidate",
        }
    evidence_refs = [
        {"digest": probe.trace_digest, "kind": probe.phase, "label": probe.label}
        for probe in probes
        if probe.status == "succeeded" and probe.trace_digest
    ]
    if not evidence_refs:
        return {**empty, "experiment_id": str(experiment.id), "reason": "probe_traces_unavailable"}
    return {
        "reserved_for": "M12",
        "triggered": True,
        "experiment_id": str(experiment.id),
        "cue": candidate["cue"],
        "lesson": candidate["lesson"],
        "domain_scope": str(candidate.get("domain_scope", "repository"))[:120],
        "conflict_key": str(candidate.get("conflict_key", experiment.id))[:120],
        "evidence_refs": evidence_refs,
        "probe_outcome": "baseline_candidate_composed_succeeded",
    }


async def _bounded_git_diff(
    source: Path, base_sha: str, head_sha: str, *, pathspec: str | None = None
) -> str:
    args = [
        "git",
        "-C",
        str(source),
        "--no-pager",
        "diff",
        "--no-ext-diff",
        "--unified=3",
        base_sha,
        head_sha,
    ]
    if pathspec:
        args.extend(["--", pathspec])
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        return "[diff unavailable: git timed out]"
    if process.returncode:
        return "[diff unavailable: revision could not be compared]"
    if len(stdout) > 1_000_000:
        return stdout[:1_000_000].decode("utf-8", errors="replace") + "\n[diff truncated at 1 MB]"
    return stdout.decode("utf-8", errors="replace")


async def _compose_review_head(
    source: Path,
    implementation_head: str,
    specification_diff: str,
    archive_diff: str,
    review_ref: str,
) -> str:
    """Create a pinned review candidate with the approved spec and its archive."""
    with tempfile.NamedTemporaryFile(prefix="harnessa-review-index-") as index_file:
        env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_INDEX_FILE": index_file.name,
            "GIT_AUTHOR_NAME": "Harnessa Review Preparation",
            "GIT_AUTHOR_EMAIL": "review@harnessa.invalid",
            "GIT_COMMITTER_NAME": "Harnessa Review Preparation",
            "GIT_COMMITTER_EMAIL": "review@harnessa.invalid",
        }

        async def git(*args: str, input_bytes: bytes | None = None) -> bytes:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(source),
                *args,
                stdin=asyncio.subprocess.PIPE
                if input_bytes is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(input_bytes), timeout=60
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise SpecificationWorkspaceError(
                    "OpenSpec review head preparation timed out"
                ) from exc
            if process.returncode:
                raise SpecificationWorkspaceError(
                    "OpenSpec archive could not be applied to the implementation head"
                )
            return stdout.strip()

        await git("read-tree", implementation_head)
        for diff in (specification_diff, archive_diff):
            if diff:
                await git("apply", "--cached", "--binary", "-", input_bytes=diff.encode())
        tree = (await git("write-tree")).decode("ascii")
        if not specification_diff and not archive_diff:
            commit = implementation_head.encode("ascii")
        else:
            commit = await git(
                "commit-tree",
                tree,
                "-p",
                implementation_head,
                "-m",
                "Prepare OpenSpec archive for review",
            )
        final_head = commit.decode("ascii")
        await git("update-ref", review_ref, final_head)
        return final_head


def _lease_is_current(lease: TaskLease | None, owner: str, token: int, now: datetime) -> bool:
    return bool(
        lease
        and lease.owner_id == owner
        and lease.fencing_token == token
        and lease.expires_at is not None
        and lease.expires_at > now
    )


def _bind_verified_review_head(
    sync_preview: dict[str, Any], expected_head: str, verified_head: str | None
) -> bool:
    verified = verified_head is not None and verified_head == expected_head
    sync_preview["final_head_verified"] = verified
    return verified


class VerificationDispatcher:
    """Claim VERIFYING tasks with a lease and persist exact verifier evidence."""

    def __init__(
        self,
        repository_sources: dict[str, Path],
        *,
        sessions: async_sessionmaker[AsyncSession] = SessionLocal,
        lease_seconds: int = 600,
    ) -> None:
        self.repository_sources = repository_sources
        self.sessions = sessions
        self.lease_seconds = max(lease_seconds, settings.run_max_wall_seconds + 60)
        self.workspace_manager = specification_workspace_manager()
        self.artifact_store = LocalArtifactStore(Path(settings.artifact_root))

    async def run_once(self) -> bool:
        owner = f"verification:{uuid4()}"
        async with self.sessions() as db:
            now = datetime.now(UTC)
            task = await db.scalar(
                select(Task)
                .where(Task.stage == TaskStage.VERIFYING.value, Task.cancel_requested_at.is_(None))
                .order_by(Task.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                return False
            lease = await db.scalar(
                select(TaskLease).where(TaskLease.task_id == task.id).with_for_update()
            )
            if lease and lease.owner_id and lease.expires_at and lease.expires_at > now:
                await db.rollback()
                return False
            run = await db.scalar(
                select(Run)
                .where(Run.task_id == task.id, Run.status == "SUCCEEDED")
                .order_by(Run.attempt.desc())
                .limit(1)
                .with_for_update()
            )
            project = await db.get(Project, task.project_id)
            if lease is None:
                lease = TaskLease(task_id=task.id, fencing_token=0)
                db.add(lease)
            lease.fencing_token += 1
            token = lease.fencing_token
            lease.run_id = run.id if run else None
            lease.owner_id = owner
            lease.heartbeat_at = now
            lease.expires_at = now + timedelta(seconds=self.lease_seconds)
            await db.commit()

        if run is None or project is None:
            return await self._record_unverified(
                task.id, owner, token, "Completed run or project is missing"
            )

        async with self.sessions() as db:
            run = await db.get(Run, run.id)
            interview = await db.scalar(
                select(SpecificationInterview).where(SpecificationInterview.task_id == task.id)
            )
            repository = await db.get(Repository, interview.repository_id) if interview else None
            policy = await db.scalar(
                select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project.id)
            )
            manifest_row = (
                await db.get(VerificationManifestRevision, run.verification_manifest_revision_id)
                if run and run.verification_manifest_revision_id
                else None
            )
        if (
            run is None
            or interview is None
            or repository is None
            or manifest_row is None
            or policy is None
            or not run.source_revision
        ):
            return await self._record_unverified(
                task.id,
                owner,
                token,
                "Run is missing its exact source, repository, or approved manifest",
            )

        review_head: str | None = None
        sync_preview: dict[str, Any] | None = None
        approved_spec_diff = ""
        try:
            current_base = await self.workspace_manager.current_base(repository)
            if current_base != run.base_sha or manifest_row.source_revision != run.base_sha:
                return await self._record_unverified(
                    task.id,
                    owner,
                    token,
                    "Repository base or approved manifest changed after dispatch",
                    run=run,
                    repository=repository,
                    manifest_row=manifest_row,
                )
            source = self.repository_sources.get(str(project.id))
            if source is None:
                return await self._record_unverified(
                    task.id,
                    owner,
                    token,
                    "No implementation repository source is configured for independent verification",
                    run=run,
                    repository=repository,
                    manifest_row=manifest_row,
                )
            source = source.expanduser().resolve(strict=True)
            specification_source = await self.workspace_manager.source_path(repository)
            if source != specification_source.resolve(strict=True):
                return await self._record_unverified(
                    task.id,
                    owner,
                    token,
                    "Implementation and specification repository source paths do not match",
                    run=run,
                    repository=repository,
                    manifest_row=manifest_row,
                )
            manifest = VerificationManifest.model_validate(manifest_row.manifest)
            if manifest.digest != manifest_row.manifest_digest:
                return await self._record_unverified(
                    task.id,
                    owner,
                    token,
                    "Approved verification manifest snapshot failed its digest check",
                    run=run,
                    repository=repository,
                    manifest_row=manifest_row,
                )
            protected_fixtures = {
                path: base64.b64decode(content, validate=True)
                for path, content in manifest_row.protected_fixtures.items()
            }
            approved_workspace = await self.workspace_manager.open(
                interview.id,
                repository,
                change_id=interview.change_id,
                base_sha=interview.base_sha,
                head_sha=interview.head_sha,
            )
            approved_spec_diff = await self.workspace_manager.artifact_diff(
                approved_workspace, interview.base_sha, interview.head_sha
            )
            archive_preview = await self.workspace_manager.archive_preview(approved_workspace)
            review_ref = f"refs/harnessa/review-candidates/{run.id.hex}-f{token}"
            review_head = await _compose_review_head(
                source,
                run.source_revision,
                approved_spec_diff,
                archive_preview["diff"],
                review_ref,
            )
            sync_preview = {
                "policy": "review_branch",
                "archive_preview_ready": True,
                "expected_base_sha": run.base_sha,
                "specification_commit_sha": run.specification_commit_sha,
                "implementation_head_sha": run.source_revision,
                "review_candidate_ref": review_ref,
                "proposed_final_review_head": review_head,
                "archive_command_output": archive_preview["command_output"],
                "archive_diff": archive_preview["diff"],
                "diff_sha256": hashlib.sha256(archive_preview["diff"].encode()).hexdigest(),
                "final_head_verified": False,
            }
            elapsed = (datetime.now(UTC) - (run.started_at or datetime.now(UTC))).total_seconds()
            shared_limit = min(settings.run_max_wall_seconds, policy.max_wall_seconds)
            remaining = int(shared_limit - elapsed)
            if remaining <= 0:
                return await self._record_unverified(
                    task.id,
                    owner,
                    token,
                    "Shared implementation and verification time limit is exhausted",
                    run=run,
                    repository=repository,
                    manifest_row=manifest_row,
                    review_head=review_head,
                    sync_preview=sync_preview,
                )
            result = await VerificationRunner(
                PodmanCommandExecutor(workspace_bytes=settings.verification_workspace_limit_bytes),
                max_total_seconds=remaining,
            ).run(
                manifest,
                repository_path=source,
                source_revision=review_head,
                protected_fixtures=protected_fixtures,
            )
            if not _bind_verified_review_head(sync_preview, review_head, result.source_revision):
                raise SpecificationWorkspaceError(
                    "Verification result did not bind to the composed review head"
                )
        except (OSError, ValueError, VerificationUnavailable, SpecificationWorkspaceError) as exc:
            return await self._record_unverified(
                task.id,
                owner,
                token,
                f"Verification could not run: {str(exc)[:300]}",
                run=run,
                repository=repository,
                manifest_row=manifest_row,
                review_head=review_head,
                sync_preview=sync_preview,
            )

        payload = {
            "source_revision": result.source_revision,
            "implementation_head_sha": run.source_revision,
            "openspec_sync_preview": sync_preview,
            "approved_specification_diff": approved_spec_diff,
            "manifest_revision": result.manifest_revision,
            "manifest_digest": result.manifest_digest,
            "outcome": result.outcome,
            "commands": [item.__dict__ for item in result.commands],
            "criteria": [item.__dict__ for item in result.criteria],
            "content_hash": result.content_hash,
        }
        payload["content_hash"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "content_hash"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        stored = await __import__("asyncio").to_thread(
            self.artifact_store.put_bytes,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            media_type="application/vnd.harnessa.verification+json",
        )
        await self._finish(task.id, run, repository, manifest_row, owner, token, payload, stored)
        return True

    async def _record_unverified(
        self,
        task_id: Any,
        owner: str,
        token: int,
        reason: str,
        *,
        run: Run | None = None,
        repository: Repository | None = None,
        manifest_row: VerificationManifestRevision | None = None,
        review_head: str | None = None,
        sync_preview: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "outcome": "unverified",
            "reason": reason[:300],
            "source_revision": review_head or (run.source_revision if run else None),
            "implementation_head_sha": run.source_revision if run else None,
            "manifest_revision": manifest_row.revision if manifest_row else None,
            "manifest_digest": manifest_row.manifest_digest if manifest_row else None,
            "commands": [],
            "criteria": [],
        }
        if sync_preview is not None:
            _bind_verified_review_head(sync_preview, review_head or "", None)
            payload["openspec_sync_preview"] = sync_preview
        if run is None or repository is None or manifest_row is None:
            async with self.sessions() as db:
                task = await db.get(Task, task_id)
                lease = await db.scalar(
                    select(TaskLease).where(TaskLease.task_id == task_id).with_for_update()
                )
                if task and _lease_is_current(lease, owner, token, datetime.now(UTC)):
                    assert lease is not None
                    before = task.version
                    old_status = task.run_status
                    lease.owner_id = None
                    lease.expires_at = datetime.now(UTC)
                    task.stage = TaskStage.HUMAN_REVIEW.value
                    task.blocked_reason = "verification_unverified"
                    task.version += 1
                    db.add(
                        TaskTransition(
                            task_id=task.id,
                            project_id=task.project_id,
                            actor_id=None,
                            from_stage=TaskStage.VERIFYING.value,
                            to_stage=TaskStage.HUMAN_REVIEW.value,
                            from_run_status=old_status,
                            to_run_status=old_status,
                            reason=reason[:300],
                            command="verification_unverified",
                            version_before=before,
                            version_after=task.version,
                        )
                    )
                    await _record_event(
                        db,
                        task=task,
                        event_type="task.verification_unverified",
                        actor_id=None,
                        payload={"reason": reason[:300], "version": task.version},
                    )
                    await db.commit()
            return True
        approved_manifest = VerificationManifest.model_validate(manifest_row.manifest)
        payload["commands"] = [
            {
                "command_id": command.id,
                "image": command.image,
                "exit_code": None,
                "timed_out": False,
                "stdout": "",
                "stderr": reason[:300],
                "duration_ms": 0,
                "outcome": "unverified",
            }
            for command in approved_manifest.commands
        ]
        payload["criteria"] = [
            {
                "criterion_id": criterion.id,
                "outcome": "unverified",
                "summary": reason[:300],
                "command_ids": [
                    command.id
                    for command in approved_manifest.commands
                    if criterion.id in command.criteria
                ],
            }
            for criterion in approved_manifest.criteria
        ]
        payload["content_hash"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        stored = await __import__("asyncio").to_thread(
            self.artifact_store.put_bytes,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            media_type="application/vnd.harnessa.verification+json",
        )
        await self._finish(task_id, run, repository, manifest_row, owner, token, payload, stored)
        return True

    async def _finish(
        self,
        task_id: Any,
        run: Run,
        repository: Repository,
        manifest: VerificationManifestRevision,
        owner: str,
        token: int,
        payload: dict[str, Any],
        artifact: StoredArtifact,
    ) -> None:
        async with self.sessions() as db:
            now = datetime.now(UTC)
            task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            lease = await db.scalar(
                select(TaskLease).where(TaskLease.task_id == task_id).with_for_update()
            )
            current_run = await db.get(Run, run.id)
            project = await db.get(Project, task.project_id) if task else None
            policy = (
                await db.scalar(
                    select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project.id)
                )
                if project
                else None
            )
            if (
                not task
                or not _lease_is_current(lease, owner, token, now)
                or task.stage != TaskStage.VERIFYING.value
                or current_run is None
                or project is None
                or policy is None
                or current_run.source_revision
                != payload.get("implementation_head_sha", current_run.source_revision)
            ):
                await db.rollback()
                return
            assert lease is not None
            evidence = VerificationExecution(
                organization_id=project.organization_id,
                project_id=task.project_id,
                repository_id=repository.id,
                task_id=task.id,
                run_id=run.id,
                manifest_revision_id=manifest.id,
                attempt=run.attempt,
                fencing_token=run.fencing_token,
                source_revision=payload.get("source_revision") or run.source_revision or "0" * 40,
                outcome=payload["outcome"],
                result=payload,
                content_hash=payload.get("content_hash")
                or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            )
            db.add(evidence)
            db.add(
                RunArtifact(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    task_id=task.id,
                    run_id=run.id,
                    fencing_token=run.fencing_token,
                    digest=artifact.digest,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    storage_key=artifact.storage_key,
                )
            )
            await db.flush()
            lease.owner_id = None
            lease.expires_at = now
            before = task.version
            old_status = task.run_status
            attempt_limit = min(settings.run_max_attempts, policy.max_attempts)
            wall_limit = min(settings.run_max_wall_seconds, policy.max_wall_seconds)
            if payload["outcome"] == "failed" and run.attempt < attempt_limit:
                first_started = await db.scalar(
                    select(func.min(Run.started_at)).where(Run.task_id == task.id)
                )
                elapsed = (datetime.now(UTC) - (first_started or datetime.now(UTC))).total_seconds()
                if elapsed < wall_limit:
                    task.stage = TaskStage.READY.value
                    task.run_status = "QUEUED"
                    task.blocked_reason = "verification_repair"
                else:
                    task.stage = TaskStage.HUMAN_REVIEW.value
                    task.blocked_reason = "verification_repair_limits_exhausted"
            else:
                task.stage = TaskStage.HUMAN_REVIEW.value
                task.blocked_reason = (
                    None if payload["outcome"] == "passed" else "verification_" + payload["outcome"]
                )
            task.version += 1
            db.add(
                TaskTransition(
                    task_id=task.id,
                    project_id=task.project_id,
                    actor_id=None,
                    from_stage=TaskStage.VERIFYING.value,
                    to_stage=task.stage,
                    from_run_status=old_status,
                    to_run_status=task.run_status,
                    reason=f"Independent verification completed with outcome {payload['outcome']}",
                    command="verification_completed",
                    version_before=before,
                    version_after=task.version,
                )
            )
            await _record_event(
                db,
                task=task,
                event_type="task.verification_completed",
                actor_id=None,
                run_id=run.id,
                payload={
                    "outcome": payload["outcome"],
                    "verification_id": str(evidence.id),
                    "artifact_digest": artifact.digest,
                    "source_revision": payload.get("source_revision", run.source_revision),
                },
            )
            if payload["outcome"] in {"passed", "failed", "manual_review_required", "unverified"}:
                source = await self.workspace_manager.source_path(repository)
                reservations = (
                    await db.scalars(
                        select(ModelReservation).where(ModelReservation.run_id == run.id)
                    )
                ).all()
                cost_evidence = [
                    {
                        "request_id": str(item.request_id),
                        "status": item.status,
                        "reserved_usd": str(item.reserved_usd),
                        "per_attempt_usd": str(item.per_attempt_usd),
                        "reserved_tokens": item.reserved_tokens,
                        "per_attempt_tokens": item.per_attempt_tokens,
                        "workload": item.workload,
                        "deployment_id": str(item.deployment_id),
                    }
                    for item in reservations
                ]
                model_events = (
                    await db.scalars(
                        select(ModelRequestLedger)
                        .where(ModelRequestLedger.run_id == run.id)
                        .order_by(ModelRequestLedger.created_at, ModelRequestLedger.attempt)
                    )
                ).all()
                model_evidence = [
                    {
                        "request_id": str(item.request_id),
                        "attempt": item.attempt,
                        "event_type": item.event_type,
                        "pool": item.pool_name,
                        "alias": item.model_alias,
                        "actual_model": item.actual_model,
                        "deployment_id": str(item.deployment_id) if item.deployment_id else None,
                        "route_decision": item.route_decision,
                        "price_revision": item.price_revision,
                        "input_tokens": item.input_tokens,
                        "output_tokens": item.output_tokens,
                        "usage_known": item.usage_known,
                        "billed_usd": str(item.billed_usd) if item.billed_usd is not None else None,
                        "estimated_usd": str(item.estimated_usd)
                        if item.estimated_usd is not None
                        else None,
                        "error_code": item.error_code,
                    }
                    for item in model_events
                ]
                review_head = payload.get("source_revision", run.source_revision or "")
                code_diff, spec_diff = await asyncio.gather(
                    _bounded_git_diff(source, run.base_sha or "", review_head),
                    _bounded_git_diff(source, run.base_sha or "", review_head, pathspec="openspec"),
                )
                interview = await db.scalar(
                    select(SpecificationInterview).where(SpecificationInterview.task_id == task.id)
                )
                approved_spec_diff = ""
                openspec_sync_preview = payload.get("openspec_sync_preview")
                if interview is not None:
                    try:
                        approved_workspace = await self.workspace_manager.open(
                            interview.id,
                            repository,
                            change_id=interview.change_id,
                            base_sha=interview.base_sha,
                            head_sha=interview.head_sha,
                        )
                        approved_spec_diff = await self.workspace_manager.artifact_diff(
                            approved_workspace, interview.base_sha, interview.head_sha
                        )
                    except (OSError, ValueError, SpecificationWorkspaceError):
                        approved_spec_diff = "[approved OpenSpec diff could not be materialized]"
                if not isinstance(openspec_sync_preview, dict):
                    openspec_sync_preview = {
                        "policy": "review_branch",
                        "archive_preview_ready": False,
                        "expected_base_sha": run.base_sha,
                        "specification_commit_sha": run.specification_commit_sha,
                        "implementation_head_sha": run.source_revision,
                        "review_candidate_ref": None,
                        "proposed_final_review_head": None,
                        "archive_command_output": "[OpenSpec archive preview was not prepared]",
                        "archive_diff": "",
                        "diff_sha256": hashlib.sha256(b"").hexdigest(),
                        "final_head_verified": False,
                    }
                harness_policy = await db.scalar(
                    select(HarnessPolicy).where(HarnessPolicy.project_id == project.id)
                )
                harness_experiment = await db.scalar(
                    select(HarnessExperiment)
                    .where(
                        HarnessExperiment.task_id == task.id,
                        HarnessExperiment.status == "succeeded",
                        HarnessExperiment.base_sha == run.base_sha,
                        HarnessExperiment.specification_hash == run.specification_hash,
                        HarnessExperiment.specification_revision == run.specification_revision,
                        HarnessExperiment.policy_revision
                        == (harness_policy.revision if harness_policy is not None else -1),
                    )
                    .order_by(HarnessExperiment.completed_at.desc())
                    .limit(1)
                )
                harness_probes = (
                    list(
                        await db.scalars(
                            select(HarnessProbeRun).where(
                                HarnessProbeRun.experiment_id == harness_experiment.id
                            )
                        )
                    )
                    if harness_experiment is not None
                    else []
                )
                harness_learning_hook = _harness_learning_hook(
                    harness_experiment,
                    harness_probes,
                    base_sha=run.base_sha,
                    specification_hash=run.specification_hash,
                    specification_revision=run.specification_revision,
                    policy_revision=(harness_policy.revision if harness_policy is not None else None),
                )
                bundle = {
                    "base_sha": run.base_sha,
                    "head_sha": review_head,
                    "implementation_head_sha": run.source_revision,
                    "specification_hash": run.specification_hash,
                    "specification_revision": run.specification_revision,
                    "policy_revision": run.policy_revision,
                    "harness_revision": run.harness_revision,
                    "harness_digest": run.harness_digest,
                    "agent_image": run.agent_image,
                    "manifest_digest": manifest.manifest_digest,
                    "verification": payload,
                    "execution": run.execution_result or {},
                    "model_cost_reservations": cost_evidence,
                    "model_route_events": model_evidence,
                    "artifact_digest": artifact.digest,
                    "code_diff": code_diff,
                    "openspec_diff": spec_diff,
                    "approved_specification_diff": payload.get(
                        "approved_specification_diff", approved_spec_diff
                    ),
                    "openspec_sync_preview": openspec_sync_preview,
                    "harness_adaptation_hook": harness_learning_hook,
                }
                bundle_bytes = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
                bundle_digest = hashlib.sha256(bundle_bytes).hexdigest()
                stored_bundle = await asyncio.to_thread(
                    self.artifact_store.put_bytes,
                    bundle_bytes,
                    media_type="application/vnd.harnessa.review-bundle+json",
                )
                db.add(
                    RunArtifact(
                        organization_id=project.organization_id,
                        project_id=project.id,
                        task_id=task.id,
                        run_id=run.id,
                        fencing_token=run.fencing_token,
                        digest=stored_bundle.digest,
                        media_type=stored_bundle.media_type,
                        size_bytes=stored_bundle.size_bytes,
                        storage_key=stored_bundle.storage_key,
                    )
                )
                db.add(
                    ReviewBundle(
                        organization_id=project.organization_id,
                        project_id=task.project_id,
                        repository_id=repository.id,
                        task_id=task.id,
                        run_id=run.id,
                        verification_execution_id=evidence.id,
                        fencing_token=run.fencing_token,
                        base_sha=run.base_sha or "0" * 40,
                        head_sha=review_head or "0" * 40,
                        specification_hash=run.specification_hash or "0" * 64,
                        specification_revision=run.specification_revision or 1,
                        harness_revision=run.harness_revision,
                        manifest_digest=manifest.manifest_digest,
                        bundle_digest=bundle_digest,
                        artifact_digest=stored_bundle.digest,
                        bundle=bundle,
                        status="prepared",
                    )
                )
            await db.commit()
