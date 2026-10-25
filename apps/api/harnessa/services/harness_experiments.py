"""Durable Harness² queue consumer backed by Harnessa's isolated ACP runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from harness2.core import schedule as harness_schedule  # type: ignore[import-untyped]
from harness2.core import tree as harness_tree
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec  # type: ignore[import-untyped]
from harness2.substrate.opencode import OpencodeSubstrate  # type: ignore[import-untyped]
from harnessa.adapters.acp_execution import ACPExecutionAdapter
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.async_bridge import call_on_event_loop
from harnessa.adapters.execution import ExecutionFailure, RunInput, StaleLease, TaskInput
from harnessa.adapters.harness2 import component_diff, validate_product_tree
from harnessa.adapters.protocols import HarnessEvaluator
from harnessa.adapters.safe_diagnostics import safe_exception_type_chain
from harnessa.adapters.specification_workspace import SpecificationWorkspaceError
from harnessa.config import settings
from harnessa.contracts import ModelPoolKind
from harnessa.db.harness import HarnessExperience, HarnessExperiment, HarnessPolicy, HarnessProbeRun
from harnessa.db.model_gateway import ModelBudgetPolicy, ModelRequestLedger
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import Run, RunArtifact, RunCheckpoint
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.services.harness_adaptation import (
    FrozenExperiment,
    GatewayHarness2Proposer,
    HarnessAdaptationError,
    _is_holdout_path,
    run_upstream_task,
)
from harnessa.services.harness_experience import (
    canonical_experience_evidence_refs,
    experience_candidate_digest,
)
from harnessa.services.specifications import specification_workspace_manager
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RUN_NAMESPACE = UUID("d7f1cfc3-bd1f-41fc-90f7-bd06482e154d")
_SURFACE = OpencodeSubstrate().surface()
_BENCH_SPEC = BenchSpec(
    name="harnessa",
    runs_root=Path("."),
    install_mode="workspace_agents_md",
    prompts=BenchPrompts(work="Complete the frozen repository task using visible inputs."),
    channels={
        "proposer": ChannelSpec(kind="opencode", model="harnessa/budgeted"),
        "selector": ChannelSpec(kind="opencode", model="harnessa/budgeted"),
    },
    allow_subagents=False,
)


def _experience_candidate_draft(
    result: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    base_tree: dict[str, str],
    task_title: str,
    repository_scope: str,
    artifact_store: LocalArtifactStore,
) -> dict[str, Any] | None:
    """Capture one selected, changed candidate as review-only CAS evidence."""
    selections = result.get("selections")
    if not isinstance(selections, dict):
        return None
    for mode, selection in selections.items():
        if not isinstance(selection, dict):
            continue
        chosen = selection.get("chosen_label")
        if not isinstance(chosen, str) or not chosen:
            continue
        record = next(
            (
                item
                for item in records
                if item.get("phase") == "candidate"
                and item.get("status") == "succeeded"
                and item.get("delivered") is True
                and item.get("source_run_id") == chosen
                and isinstance(item.get("tree"), dict)
            ),
            None,
        )
        if record is None:
            continue
        tree = record["tree"]
        changed_paths = sorted(
            path
            for path in set(base_tree) | set(tree)
            if base_tree.get(path) != tree.get(path)
        )
        if not changed_paths:
            continue
        stage_rows = (result.get("modes", {}).get(mode, {}) or {}).get("stages", [])
        note = next(
            (
                stage.get("notes", "").strip()
                for stage in stage_rows
                if isinstance(stage, dict)
                and stage.get("label") == chosen
                and isinstance(stage.get("notes"), str)
                and stage["notes"].strip()
            ),
            "",
        )
        lesson = note[:2000] if note else (
            "Review the selected Harness² component changes in "
            + ", ".join(changed_paths[:20])
            + ". This candidate remains inactive until independent evaluation and operator review."
        )
        component_bytes = json.dumps(
            {"schema_version": 1, "components": tree},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        stored = artifact_store.put_bytes(
            component_bytes,
            media_type="application/vnd.harnessa.harness-candidate+json",
        )
        conflict_material = f"{repository_scope}:{task_title.strip().casefold()}"
        return {
            "cue": task_title[:1000],
            "lesson": lesson,
            "repository_scope": repository_scope,
            "domain_scope": "repository",
            "conflict_key": hashlib.sha256(conflict_material.encode()).hexdigest()[:32],
            "component_digest": stored.digest,
            "component_artifact": {
                "sha256": stored.digest,
                "storage_key": stored.storage_key,
                "size_bytes": stored.size_bytes,
                "media_type": stored.media_type,
            },
            "changed_components": changed_paths[:20],
            "review_required": True,
            "automatically_activated": False,
        }
    return None


def _exception_type_chain(error: BaseException, *, limit: int = 4) -> str:
    """Return exception classes and safe protocol codes, never exception messages."""
    return safe_exception_type_chain(error, limit=limit)


class HarnessACPTaskAdapter:
    """Harness² rollouts executed by independent, fenced Harnessa ACP runs."""

    def __init__(
        self,
        *,
        frozen: FrozenExperiment,
        execution: ACPExecutionAdapter,
        task: TaskInput,
        organization_id: UUID,
        repository_scope: str,
        base_harness: Path,
        max_concurrency: int,
        event_loop: asyncio.AbstractEventLoop,
        sessions: async_sessionmaker[AsyncSession] = SessionLocal,
    ) -> None:
        self.frozen = frozen
        self.execution = execution
        self.task_input = task
        self.organization_id = organization_id
        self.repository_scope = repository_scope
        self._base_harness = base_harness
        self._event_loop = event_loop
        self._sessions = sessions
        self.substrate = OpencodeSubstrate()
        self.spec = _BENCH_SPEC
        self.records: list[dict[str, Any]] = []
        self._events_by_run: dict[UUID, list[dict[str, Any]]] = {}
        self._records_lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(max(1, min(max_concurrency, 3)))
        self._active_runs: set[UUID] = set()
        self._active_lock = threading.Lock()

    def tasks(self, split: str = "all") -> list[str]:
        return [str(self.frozen.task_id)] if split == "all" else []

    def domain_of(self, task_id: str) -> str:
        if task_id != str(self.frozen.task_id):
            raise ValueError("Harness² adapter received an unbound task")
        return self.repository_scope

    def task_prompt(self, task_id: str) -> str:
        self.domain_of(task_id)
        return f"{self.task_input.title}\n\n{self.task_input.description[:20_000]}"

    def task_files(self, task_id: str) -> None:
        self.domain_of(task_id)

    def base_harness(self) -> Path:
        return self._base_harness

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        self.domain_of(task_id)
        del timeout
        run_uuid = uuid5(_RUN_NAMESPACE, f"{self.frozen.experiment_id}:{self.frozen.claim_attempt}:{run_id}")
        run_dir = self.frozen.root / "rollouts" / run_uuid.hex
        with self._semaphore:
            if not (run_dir / "outcome.json").is_file():
                started = time.monotonic()
                try:
                    result = call_on_event_loop(
                        self._execute(run_uuid, harness_dir or self._base_harness), self._event_loop
                    )
                except ExecutionFailure as exc:
                    # Preserve a bounded, non-content diagnostic so failed model
                    # rollouts remain comparable and debuggable without storing
                    # prompts, provider responses, or exception messages.
                    result = {
                        "status": "failed",
                        "error_code": exc.code[:100],
                        "failure_type": _exception_type_chain(exc),
                    }
                except (HarnessAdaptationError, StaleLease):
                    raise
                except Exception as exc:  # noqa: BLE001 - redact adapter internals at this boundary.
                    result = {"status": "failed", "error_code": type(exc).__name__[:100]}
                elapsed_ms = int((time.monotonic() - started) * 1000)
                run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                artifact = result.get("patch_artifact")
                patch = b""
                if isinstance(artifact, dict) and isinstance(artifact.get("storage_key"), str):
                    stored_path = Path(settings.artifact_root) / artifact["storage_key"]
                    if stored_path.is_file() and not stored_path.is_symlink():
                        patch = stored_path.read_bytes()[: settings.acp_artifact_limit_bytes]
                if patch:
                    (run_dir / "deliverable.patch").write_bytes(patch)
                (run_dir / "outcome.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
                events = self._events_by_run.get(run_uuid, [])
                (run_dir / "trajectory.txt").write_text(
                    json.dumps({
                        "status": result.get("status"),
                        "changed_files": result.get("changed_files", []),
                        "patch_sha256": result.get("patch_sha256"),
                        "events": events,
                    }, sort_keys=True),
                    encoding="utf-8",
                )
                harness_tree_value = harness_tree.read_tree(self.substrate.surface(), harness_dir or self._base_harness)
                validate_product_tree(harness_tree_value)
                digest = hashlib.sha256(json.dumps(harness_tree_value, sort_keys=True).encode()).hexdigest()
                with self._records_lock:
                    self.records.append({
                        "label": _probe_label(run_id),
                        "source_run_id": run_id,
                        "run_uuid": str(run_uuid),
                        "phase": "baseline" if harness_dir is None else "candidate",
                        "status": (
                            "succeeded" if result.get("status") == "completed"
                            else "cancelled" if result.get("status") == "cancelled"
                            else "failed"
                        ),
                        "delivered": bool(patch),
                        "wall_ms": elapsed_ms,
                        "harness_digest": digest,
                        "tree": harness_tree_value,
                        "run_dir": str(run_dir),
                        "outcome": json.dumps(result, sort_keys=True),
                    })
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        try:
            result = json.loads((run_dir / "outcome.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return result.get("status") == "completed" and (run_dir / "deliverable.patch").is_file()

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        return (run_dir / "trajectory.txt").read_text(encoding="utf-8")[:obs_trunc]

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        path = run_dir / "deliverable.patch"
        return {path.name: path.read_text(encoding="utf-8", errors="replace")} if path.is_file() else {}

    def deliverable_text(self, run_dir: Path) -> str:
        return next(iter(self.deliverable_files(run_dir).values()), "")

    async def cancel_active(self) -> None:
        with self._active_lock:
            run_ids = list(self._active_runs)
        await asyncio.gather(*(self.execution.cancel(run_id) for run_id in run_ids))

    async def _execute(self, run_id: UUID, harness_dir: Path) -> dict[str, Any]:
        frozen = self.frozen
        instructions = _flatten_harness(harness_dir)
        now = datetime.now(UTC)
        async with self._sessions() as db:
            experiment = await db.scalar(select(HarnessExperiment).where(
                HarnessExperiment.id == frozen.experiment_id
            ).with_for_update())
            project = await db.get(Project, frozen.project_id)
            task = await db.scalar(select(Task).where(Task.id == frozen.task_id).with_for_update())
            policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == frozen.project_id))
            if experiment is None or project is None or task is None or policy is None or policy.revision != frozen.policy_revision or not _experiment_current(experiment, frozen):
                raise HarnessAdaptationError("Experiment fencing token is no longer current")
            existing = await db.get(Run, run_id)
            if existing is not None:
                raise HarnessAdaptationError("Rollout run id already exists without a durable outcome")
            attempt = int(await db.scalar(select(func.max(Run.attempt)).where(Run.task_id == task.id)) or 0) + 1
            row = Run(
                id=run_id, task_id=task.id, project_id=project.id, attempt=attempt,
                status="RUNNING", fencing_token=frozen.claim_attempt, started_at=now,
                heartbeat_at=now, deadline_at=now + timedelta(seconds=settings.run_max_wall_seconds),
                adapter_name="harness2-probe", base_sha=frozen.base_sha,
                specification_hash=frozen.specification_hash,
                specification_revision=frozen.specification_revision,
                policy_revision=str(frozen.policy_revision), harness_revision=frozen.tool_revision,
                harness_digest=hashlib.sha256(instructions.encode()).hexdigest(),
                agent_image=frozen.image_revision, execution_result={"experiment_id": str(frozen.experiment_id)},
            )
            db.add(row)
            await db.commit()

        task_input = TaskInput(
            id=self.task_input.id, project_id=self.task_input.project_id,
            title=self.task_input.title, description=self.task_input.description,
            approved_base_sha=frozen.base_sha, specification_hash=frozen.specification_hash,
            specification_revision=frozen.specification_revision,
            harness_instructions=instructions, harness_revision=frozen.tool_revision,
            harness_digest=hashlib.sha256(instructions.encode()).hexdigest(),
        )
        run_input = RunInput(
            run_id, attempt, frozen.claim_attempt, model_pool=ModelPoolKind.PROBE,
            model_alias=frozen.route_alias, persist_ui_events=False,
            probe_event_sink=self._event_sink(run_id),
        )

        async def current(candidate_id: UUID, token: int) -> bool:
            if candidate_id != run_id or token != frozen.claim_attempt:
                return False
            async with self._sessions() as db:
                exp = await db.get(HarnessExperiment, frozen.experiment_id)
                run = await db.get(Run, run_id)
                policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == frozen.project_id))
                return bool(exp and run and policy and policy.revision == frozen.policy_revision and _experiment_current(exp, frozen) and run.status == "RUNNING")

        async def cancelled() -> bool:
            async with self._sessions() as db:
                exp = await db.get(HarnessExperiment, frozen.experiment_id)
                return exp is None or exp.cancel_requested_at is not None or exp.status != "running"

        async def checkpoint(state: Mapping[str, Any]) -> None:
            state_document = dict(state)
            state_bytes = json.dumps(state_document, sort_keys=True, separators=(",", ":")).encode()
            async with self._sessions() as db:
                if not await current(run_id, frozen.claim_attempt):
                    raise HarnessAdaptationError("Checkpoint rejected by stale experiment fence")
                sequence = int(await db.scalar(select(func.max(RunCheckpoint.sequence)).where(
                    RunCheckpoint.run_id == run_id
                )) or 0) + 1
                db.add(RunCheckpoint(
                    task_id=task_input.id, project_id=task_input.project_id, run_id=run_id,
                    sequence=sequence, fencing_token=frozen.claim_attempt, state=state_document,
                    state_hash=hashlib.sha256(state_bytes).hexdigest(), resumable=True,
                ))
                await db.commit()

        async def publish(content: bytes, media_type: str) -> dict[str, Any]:
            stored = await asyncio.to_thread(LocalArtifactStore(Path(settings.artifact_root)).put_bytes, content, media_type=media_type)
            async with self._sessions() as db:
                if not await current(run_id, frozen.claim_attempt):
                    raise HarnessAdaptationError("Artifact rejected by stale experiment fence")
                db.add(RunArtifact(
                    organization_id=self.organization_id, project_id=task_input.project_id,
                    task_id=task_input.id, run_id=run_id, fencing_token=frozen.claim_attempt,
                    digest=stored.digest, media_type=stored.media_type,
                    size_bytes=stored.size_bytes, storage_key=stored.storage_key,
                ))
                await db.commit()
            return stored.__dict__

        with self._active_lock:
            self._active_runs.add(run_id)
        try:
            result = dict(await self.execution.execute(
                task_input, run_input, checkpoint=None, save_checkpoint=checkpoint,
                is_cancelled=cancelled, is_current_fence=current, publish_artifact=publish,
            ))
            async with self._sessions() as db:
                stored_run = await db.get(Run, run_id)
                if stored_run is not None:
                    stored_run.status = "SUCCEEDED" if result.get("status") == "completed" else "CANCELLED"
                    stored_run.finished_at = datetime.now(UTC)
                    stored_run.source_revision = result.get("source_revision")
                    stored_run.execution_result = result
                    await db.commit()
            return result
        except Exception as exc:
            async with self._sessions() as db:
                failed_run = await db.get(Run, run_id)
                if failed_run is not None:
                    failed_run.status = "FAILED"
                    failed_run.finished_at = datetime.now(UTC)
                    failed_run.error_code = (
                        exc.code[:80] if isinstance(exc, ExecutionFailure) else type(exc).__name__[:80]
                    )
                    failed_run.error_message = (
                        _exception_type_chain(exc)[:300]
                    )
                    await db.commit()
            raise
        finally:
            with self._active_lock:
                self._active_runs.discard(run_id)

    def _event_sink(
        self, run_id: UUID
    ) -> Callable[[Mapping[str, Any]], Awaitable[None]]:
        async def capture(event: Mapping[str, Any]) -> None:
            kind = event.get("kind")
            if not isinstance(kind, str):
                return
            record: dict[str, Any] = {"kind": kind, "at_monotonic": time.monotonic()}
            for field in ("tool_name", "outcome"):
                value = event.get(field)
                if isinstance(value, str):
                    record[field] = value[:100]
            text = event.get("text")
            if isinstance(text, str):
                record["text_chars"] = min(len(text), 1_000_000)
            with self._records_lock:
                rows = self._events_by_run.setdefault(run_id, [])
                if len(rows) < 2_000:
                    rows.append(record)

        return capture


class HarnessExperimentWorker:
    """Claim queued experiments and persist matched Harness² task-level evidence."""

    def __init__(
        self, execution: ACPExecutionAdapter | None, *,
        sessions: async_sessionmaker[AsyncSession] = SessionLocal,
        worker_id: str = "harness-experiment-worker",
        evaluator: HarnessEvaluator | None = None,
    ) -> None:
        self.execution = execution
        self._sessions = sessions
        self.worker_id = worker_id[:200]
        self.evaluator = evaluator

    async def run_once(self) -> bool:
        if self.execution is None or not self.execution.available:
            return False
        claim = await self._claim()
        if claim is None:
            return False
        experiment_id, attempt = claim
        try:
            await self._run(experiment_id, attempt)
        except Exception as exc:  # noqa: BLE001 - mark the durable experiment terminal on failure
            await self._finish(experiment_id, attempt, "cancelled" if await self._cancelled(experiment_id, attempt) else "failed", f"Experiment failed ({type(exc).__name__})")
        return True

    async def _claim(self) -> tuple[UUID, int] | None:
        now = datetime.now(UTC)
        async with self._sessions() as db:
            stale_before = now - timedelta(minutes=3)
            stale = list(await db.scalars(select(HarnessExperiment).where(
                HarnessExperiment.status == "running",
                or_(
                    HarnessExperiment.worker_heartbeat_at < stale_before,
                    HarnessExperiment.worker_heartbeat_at.is_(None)
                    & (HarnessExperiment.started_at < stale_before),
                ),
            ).with_for_update(skip_locked=True)))
            for old in stale:
                stale_runs = list(await db.scalars(select(Run).where(
                    Run.task_id == old.task_id,
                    Run.fencing_token == old.attempts,
                    Run.adapter_name.in_(("harness2-probe", "harness2-proposer")),
                    Run.status == "RUNNING",
                ).with_for_update()))
                for stale_run in stale_runs:
                    stale_run.status, stale_run.finished_at = "FAILED", now
                    stale_run.error_code = "experiment_lease_expired"
                    stale_run.error_message = "Experiment worker lease expired"
                old.status, old.phase = "queued", "queued"
                old.worker_heartbeat_at = None
                old.last_error = "Worker lease expired; retry will use a new fence"
            row = await db.scalar(select(HarnessExperiment).where(
                HarnessExperiment.status == "queued", HarnessExperiment.cancel_requested_at.is_(None)
            ).order_by(HarnessExperiment.created_at).with_for_update(skip_locked=True).limit(1))
            if row is None:
                if stale:
                    await db.commit()
                return None
            await db.scalar(select(Task).where(Task.id == row.task_id).with_for_update())
            another = await db.scalar(select(HarnessExperiment.id).where(
                HarnessExperiment.task_id == row.task_id,
                HarnessExperiment.status == "running",
            ).limit(1))
            if another is not None:
                await db.rollback()
                return None
            row.status, row.phase = "running", "baseline_candidate_composed"
            row.attempts += 1
            row.started_at, row.last_error = now, None
            row.worker_heartbeat_at = now
            row.result = {**row.result, "worker_id": self.worker_id}
            await db.commit()
            return row.id, row.attempts

    async def _run(self, experiment_id: UUID, attempt: int) -> None:
        (frozen, task_input, organization_id, repository_scope,
         base_harness, experiences, max_concurrency) = await self._inputs(experiment_id, attempt)
        assert self.execution is not None
        source = self.execution.repository_sources.get(str(frozen.project_id))
        if source is None:
            raise HarnessAdaptationError("Experiment repository source is unavailable")
        await asyncio.to_thread(_assert_holdout_free_tree, source, frozen.base_sha)
        adapter = HarnessACPTaskAdapter(
            frozen=frozen, execution=self.execution, task=task_input,
            organization_id=organization_id, repository_scope=repository_scope,
            base_harness=base_harness, max_concurrency=max_concurrency, sessions=self._sessions,
            event_loop=asyncio.get_running_loop(),
        )
        proposer = GatewayHarness2Proposer(
            frozen, max_concurrency=max_concurrency, event_loop=asyncio.get_running_loop()
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(experiment_id, attempt))
        # Libraries start from the same immutable base plus only active, unexpired,
        # repository-scoped experiences. Harness² owns candidate isolation and selection.
        library_root = frozen.root / "libraries"
        library_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        from harness2.core.library import Library  # type: ignore[import-untyped]
        libraries = {}
        for mode in ("agg", "seq"):
            lib = Library(root=library_root / mode, domain=repository_scope, surface=adapter.substrate.surface())
            base_tree = harness_tree.read_tree(adapter.substrate.surface(), base_harness)
            for experience in experiences:
                tree = dict(base_tree)
                tree["LongTermMEMORY.md"] = (tree.get("LongTermMEMORY.md", "") +
                    f"\n\n## Approved scoped experience\nCue: {experience.cue}\nLesson: {experience.lesson}")[:32_000]
                lib.add(f"approved-{experience.id}", tree, experience={"cue": experience.cue, "lesson": experience.lesson, "evidence": json.dumps(experience.evidence_refs, sort_keys=True)[:4_000]})
            libraries[mode] = lib
        runner = asyncio.create_task(asyncio.to_thread(
            run_upstream_task, adapter, proposer, task_id=str(frozen.task_id),
            mode=frozen.mode, k=frozen.k, runs_root=frozen.root / "runs",
            library_root=library_root, task_tag=f"{frozen.experiment_id}-{attempt}",
        ))
        try:
            while not runner.done():
                await asyncio.sleep(0.5)
                if await self._cancelled(experiment_id, attempt):
                    proposer.cancelled.set()
                    await adapter.cancel_active()
            result = await runner
            if await self._cancelled(experiment_id, attempt):
                await self._finish(experiment_id, attempt, "cancelled", "Cancellation requested during probe execution")
                return
            comparison_passed, comparison_issues = _matched_comparison(result, adapter.records)
            result["comparison_successful"] = comparison_passed
            result["comparison_integrity"] = {
                "status": "passed" if comparison_passed else "failed",
                "issues": comparison_issues,
            }
            await self._execute_composed(frozen, adapter, result)
            result["experience_candidate"] = await asyncio.to_thread(
                _experience_candidate_draft,
                result,
                adapter.records,
                base_tree=harness_tree.read_tree(
                    adapter.substrate.surface(), adapter.base_harness()
                ),
                task_title=task_input.title,
                repository_scope=repository_scope,
                artifact_store=LocalArtifactStore(Path(settings.artifact_root)),
            )
            await self._persist(frozen, adapter, result)
            holdout = await self._grade_holdout(frozen, result)
            successful = (
                result.get("comparison_successful") is True
                and result.get("composed_successful") is True
                and holdout.get("status") == "passed"
            )
            terminal_status = "succeeded" if successful else (
                "blocked" if holdout.get("status") in {"unavailable", "unknown"} else "failed"
            )
            error = None if successful else (
                "Independent holdout grading is unavailable"
                if terminal_status == "blocked"
                else "Baseline, candidate, composed execution, or holdout gate failed"
            )
            await self._finish(experiment_id, attempt, terminal_status, error)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not runner.done():
                proposer.cancelled.set()
                await adapter.cancel_active()
                await asyncio.gather(runner, return_exceptions=True)

    async def _inputs(
        self, experiment_id: UUID, attempt: int
    ) -> tuple[FrozenExperiment, TaskInput, UUID, str, Path, list[HarnessExperience], int]:
        async with self._sessions() as db:
            exp = await db.get(HarnessExperiment, experiment_id)
            task = (
                await db.scalar(select(Task).where(Task.id == exp.task_id).with_for_update())
                if exp else None
            )
            project = await db.get(Project, exp.project_id) if exp else None
            interview = await db.scalar(select(SpecificationInterview).where(SpecificationInterview.task_id == exp.task_id)) if exp else None
            repo = await db.get(Repository, interview.repository_id) if interview else None
            policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == exp.project_id)) if exp else None
            if not exp or not task or not project or not interview or not repo or not policy or exp.status != "running" or exp.attempts != attempt:
                raise HarnessAdaptationError("Claimed experiment or frozen task inputs are unavailable")
            if task.stage != "HARNESS_PROBE":
                raise HarnessAdaptationError("Task left HARNESS_PROBE before experiment execution")
            if interview.readiness_state != "READY" or (interview.base_sha, interview.artifact_hash, interview.revision) != (exp.base_sha, exp.specification_hash, exp.specification_revision) or policy.revision != exp.policy_revision:
                raise HarnessAdaptationError("Frozen specification or policy changed")
            manager = specification_workspace_manager()
            try:
                context = await manager.read_file_at_revision(repo, exp.base_sha, "AGENTS.md", limit_bytes=128_000)
            except SpecificationWorkspaceError:
                context = b""
            root = Path(settings.artifact_root) / "harness2" / str(exp.id) / str(attempt)
            base_harness = root / "base-harness"
            base_harness.mkdir(mode=0o700, parents=True, exist_ok=True)
            (base_harness / "systemprompt.md").write_bytes(context)
            route = str(exp.result.get("route_alias", ""))
            if not route:
                raise HarnessAdaptationError("Frozen probe route is missing")
            proposer_run_id = uuid5(_RUN_NAMESPACE, f"{exp.id}:{attempt}:proposer")
            proposer_run_attempt = int(await db.scalar(
                select(func.max(Run.attempt)).where(Run.task_id == task.id)
            ) or 0) + 1
            db.add(Run(
                id=proposer_run_id, task_id=task.id, project_id=project.id,
                attempt=proposer_run_attempt, status="RUNNING", fencing_token=attempt,
                started_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC),
                deadline_at=datetime.now(UTC) + timedelta(seconds=settings.run_max_wall_seconds * max(1, exp.k) * 8),
                adapter_name="harness2-proposer", base_sha=exp.base_sha,
                specification_hash=exp.specification_hash,
                specification_revision=exp.specification_revision,
                policy_revision=str(exp.policy_revision), harness_revision=exp.harness2_revision,
                agent_image=settings.acp_agent_image,
                execution_result={"experiment_id": str(exp.id)},
            ))
            await db.flush()
            frozen = FrozenExperiment(
                exp.id, task.id, project.id, project.organization_id, exp.base_sha,
                exp.specification_hash, exp.specification_revision, exp.policy_revision,
                exp.mode, exp.k, route, settings.acp_agent_image,
                settings.acp_harness_revision, root, attempt, proposer_run_id,
            )
            task_input = TaskInput(task.id, project.id, task.title, task.description, approved_base_sha=exp.base_sha, specification_hash=exp.specification_hash, specification_revision=exp.specification_revision)
            experiences = list(await db.scalars(select(HarnessExperience).where(HarnessExperience.project_id == exp.project_id, HarnessExperience.status == "active", HarnessExperience.repository_scope == repo.target_id, HarnessExperience.expiry_at > datetime.now(UTC))))
            await db.commit()
            return frozen, task_input, project.organization_id, repo.target_id, base_harness, experiences, min(policy.max_probe_concurrency, settings.harness2_max_probe_concurrency)

    async def _execute_composed(self, frozen: FrozenExperiment, adapter: HarnessACPTaskAdapter, result: dict[str, Any]) -> None:
        selections = result.get("selections", {})
        modes = {"parallel": "agg", "sequential": "seq"}.get(frozen.mode)
        chosen_modes = [modes] if modes else ["agg", "seq"]
        for mode in chosen_modes:
            selection = selections.get(mode, {})
            chosen_label = str(selection.get("chosen_label", ""))
            namespace = harness_schedule.Namespace(
                _BENCH_SPEC.name,
                adapter.substrate.name,
                mode,
                frozen.k,
                tag=f"{frozen.experiment_id}-{frozen.claim_attempt}",
            )
            chosen_dir = namespace.task_dir(frozen.root / "runs", str(frozen.task_id))
            harness_dir = chosen_dir / "harnesses" / chosen_label
            if not chosen_label or not harness_dir.resolve().is_relative_to(chosen_dir.resolve()) or not harness_dir.is_dir():
                harness_dir = None
            if harness_dir is None or not harness_dir.is_dir():
                result["composed_successful"] = False
                continue
            composed_id = f"composed-{mode}-{frozen.experiment_id}-{frozen.claim_attempt}"
            run_dir = await asyncio.to_thread(adapter.execute, str(frozen.task_id), harness_dir, composed_id)
            record = next((row for row in adapter.records if row["label"] == _probe_label(composed_id)), None)
            if record:
                record["phase"] = "composed"
                record["label"] = _probe_label(composed_id)
                record["component_diff"] = component_diff(
                    harness_tree.read_tree(adapter.substrate.surface(), adapter.base_harness()), record["tree"]
                )
                record["delivered"] = adapter.delivered(run_dir)
            result.setdefault("composed", {})[mode] = {"run_dir": str(run_dir), "delivered": adapter.delivered(run_dir)}
        composed = result.get("composed", {})
        result["composed_successful"] = (
            len(composed) == len(chosen_modes)
            and bool(chosen_modes)
            and all(v.get("delivered") is True for v in composed.values())
        )

    async def _persist(self, frozen: FrozenExperiment, adapter: HarnessACPTaskAdapter, result: dict[str, Any]) -> None:
        async with self._sessions() as db:
            exp = await db.scalar(select(HarnessExperiment).where(
                HarnessExperiment.id == frozen.experiment_id
            ).with_for_update())
            policy = await db.scalar(select(HarnessPolicy).where(
                HarnessPolicy.project_id == frozen.project_id
            ))
            interview = await db.scalar(select(SpecificationInterview).where(
                SpecificationInterview.task_id == frozen.task_id
            ))
            if (
                exp is None or not _experiment_current(exp, frozen)
                or policy is None or policy.revision != frozen.policy_revision
                or interview is None or interview.readiness_state != "READY"
                or (interview.base_sha, interview.artifact_hash, interview.revision)
                != (frozen.base_sha, frozen.specification_hash, frozen.specification_revision)
            ):
                return
            base_tree = harness_tree.read_tree(adapter.substrate.surface(), adapter.base_harness())
            holdout_artifacts: list[dict[str, Any]] = []
            probe_cost_total = Decimal(0)
            probe_cost_known = True
            for record in adapter.records:
                trace = (Path(record["run_dir"]) / "trajectory.txt").read_bytes()[:200_000]
                store = LocalArtifactStore(Path(settings.artifact_root))
                stored = await asyncio.to_thread(store.put_bytes, trace, media_type="application/vnd.harnessa.harness-probe-trace+text")
                metrics = list(await db.scalars(select(ModelRequestLedger).where(ModelRequestLedger.run_id == UUID(record["run_uuid"]), ModelRequestLedger.event_type == "settled")))
                attempts = list(await db.scalars(select(ModelRequestLedger).where(
                    ModelRequestLedger.run_id == UUID(record["run_uuid"]),
                    ModelRequestLedger.event_type == "attempt_succeeded",
                )))
                known_tokens = [int(x.input_tokens) + int(x.output_tokens) for x in attempts
                                if x.usage_known and x.input_tokens is not None and x.output_tokens is not None]
                known_cost = [(x.billed_usd or 0) + (x.estimated_usd or 0) for x in metrics if x.usage_known]
                if not metrics or len(known_cost) != len(metrics):
                    probe_cost_known = False
                else:
                    probe_cost_total += sum(known_cost, Decimal(0))
                run_id = UUID(record["run_uuid"])
                run_artifacts = list(await db.scalars(select(RunArtifact).where(
                    RunArtifact.run_id == run_id,
                    RunArtifact.fencing_token == frozen.claim_attempt,
                )))
                holdout_artifacts.append({
                    "phase": record["phase"],
                    "label": record["label"][:100],
                    "status": record["status"],
                    "run_id": str(run_id),
                    "harness_digest": record["harness_digest"],
                    "trace": {
                        "sha256": stored.digest,
                        "size_bytes": stored.size_bytes,
                    },
                    "deliverable": record["delivered"],
                    "artifacts": [
                        {
                            "sha256": item.digest,
                            "media_type": item.media_type,
                            "size_bytes": item.size_bytes,
                        }
                        for item in run_artifacts
                    ],
                })
                db.add(HarnessProbeRun(
                    organization_id=frozen.organization_id, project_id=frozen.project_id, task_id=frozen.task_id,
                    experiment_id=frozen.experiment_id, label=record["label"][:100], phase=record["phase"], status=record["status"],
                    delivered=record["delivered"], wall_ms=record["wall_ms"], tokens=sum(known_tokens) if metrics and len(known_tokens) == len(metrics) and len(known_tokens) == len(attempts) else None,
                    cost_usd=sum(known_cost) if metrics and len(known_cost) == len(metrics) else None, route_alias=frozen.route_alias,
                    image_revision=frozen.image_revision, tool_revision=frozen.tool_revision,
                    harness_digest=record["harness_digest"], trace_digest=stored.digest, trace_storage_key=stored.storage_key,
                    trace_size_bytes=stored.size_bytes, component_diff=record.get("component_diff", component_diff(base_tree, record["tree"])),
                    outcome_detail=record["outcome"][:4_000],
                ))
            if exp:
                prior_result = exp.result if isinstance(exp.result, dict) else {}
                prior_resume_count = prior_result.get("resume_count", 0)
                result["resume_count"] = (
                    prior_resume_count
                    if isinstance(prior_resume_count, int)
                    and not isinstance(prior_resume_count, bool)
                    and prior_resume_count >= 0
                    else 0
                )
                proposer_ledger = list(await db.scalars(select(ModelRequestLedger).where(
                    ModelRequestLedger.run_id == frozen.proposer_run_id,
                ))) if frozen.proposer_run_id else []
                proposer_metrics = [item for item in proposer_ledger if item.event_type == "settled"]
                proposer_attempts = [item for item in proposer_ledger if item.event_type == "attempt_succeeded"]
                known_proposer_usage = bool(proposer_metrics) and all(
                    item.usage_known for item in proposer_metrics
                )
                if not known_proposer_usage:
                    probe_cost_known = False
                else:
                    probe_cost_total += sum(
                        ((item.billed_usd or 0) + (item.estimated_usd or 0)
                         for item in proposer_metrics),
                        Decimal(0),
                    )
                known_proposer_tokens = [
                    int(item.input_tokens) + int(item.output_tokens)
                    for item in proposer_attempts
                    if item.usage_known and item.input_tokens is not None and item.output_tokens is not None
                ]
                known_proposer_cost = [
                    (item.billed_usd or 0) + (item.estimated_usd or 0)
                    for item in proposer_metrics if item.usage_known
                ]
                result["adaptation_usage"] = {
                    "pool": "probe",
                    "route_alias": frozen.route_alias,
                    "request_count": len(proposer_metrics),
                    "tokens": sum(known_proposer_tokens)
                    if proposer_metrics and len(known_proposer_tokens) == len(proposer_metrics)
                    and len(known_proposer_tokens) == len(proposer_attempts)
                    else None,
                    "cost_usd": str(sum(known_proposer_cost))
                    if proposer_metrics and len(known_proposer_cost) == len(proposer_metrics)
                    else None,
                    "cost_basis": "settled billed plus estimated amounts",
                    "usage_known": bool(proposer_metrics)
                    and len(known_proposer_tokens) == len(proposer_metrics)
                    and len(known_proposer_cost) == len(proposer_metrics),
                }
                budget = await db.scalar(
                    select(ModelBudgetPolicy).where(
                        ModelBudgetPolicy.project_id == frozen.project_id
                    )
                )
                probe_task_limit = budget.probe_task_limit_usd if budget else Decimal(0)
                result["cost_gate"] = {
                    "status": (
                        "passed"
                        if probe_cost_known
                        and probe_task_limit > 0
                        and probe_cost_total <= probe_task_limit
                        else "blocked"
                    ),
                    "usage_known": probe_cost_known,
                    "cost_usd": str(probe_cost_total) if probe_cost_known else None,
                    "task_limit_usd": str(probe_task_limit) if budget else None,
                }
                candidate = result.get("experience_candidate")
                candidate_metadata_artifact = None
                base_harness_bytes = json.dumps(
                    {"schema_version": 1, "components": base_tree},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                base_harness_artifact = await asyncio.to_thread(
                    LocalArtifactStore(Path(settings.artifact_root)).put_bytes,
                    base_harness_bytes,
                    media_type="application/vnd.harnessa.harness-base+json",
                )
                if isinstance(candidate, dict):
                    evidence_refs = canonical_experience_evidence_refs([
                        {
                            "digest": artifact["trace"]["sha256"],
                            "kind": artifact["phase"],
                            "label": artifact["label"],
                        }
                        for artifact in holdout_artifacts
                        if artifact.get("phase") in {"baseline", "candidate", "composed"}
                        and artifact.get("status") == "succeeded"
                        and isinstance(artifact.get("trace"), dict)
                    ])
                    candidate["evidence_refs"] = evidence_refs
                    candidate["candidate_digest"] = experience_candidate_digest(
                        cue=candidate["cue"],
                        lesson=candidate["lesson"],
                        evidence_refs=evidence_refs,
                        repository_scope=candidate["repository_scope"],
                        domain_scope=candidate["domain_scope"],
                    )
                    candidate_metadata_bytes = json.dumps(
                        {
                            "schema_version": 1,
                            "cue": candidate["cue"],
                            "lesson": candidate["lesson"],
                            "repository_scope": candidate["repository_scope"],
                            "domain_scope": candidate["domain_scope"],
                            "evidence_refs": evidence_refs,
                            "candidate_digest": candidate["candidate_digest"],
                            "component_digest": candidate["component_digest"],
                            "component_artifact": candidate["component_artifact"],
                            "changed_components": candidate["changed_components"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    candidate_metadata_artifact = await asyncio.to_thread(
                        LocalArtifactStore(Path(settings.artifact_root)).put_bytes,
                        candidate_metadata_bytes,
                        media_type="application/vnd.harnessa.experience-candidate+json",
                    )
                    candidate["manifest_artifact"] = {
                        "sha256": candidate_metadata_artifact.digest,
                        "storage_key": candidate_metadata_artifact.storage_key,
                        "size_bytes": candidate_metadata_artifact.size_bytes,
                        "media_type": candidate_metadata_artifact.media_type,
                    }
                manifest = {
                    "schema_version": 1,
                    "experiment_id": str(frozen.experiment_id),
                    "task_id": str(frozen.task_id),
                    "project_id": str(frozen.project_id),
                    "attempt": frozen.claim_attempt,
                    "base_sha": frozen.base_sha,
                    "specification_hash": frozen.specification_hash,
                    "specification_revision": frozen.specification_revision,
                    "policy_revision": frozen.policy_revision,
                    "harness2_revision": frozen.tool_revision,
                    "mode": frozen.mode,
                    "k": frozen.k,
                    "route_alias": frozen.route_alias,
                    "image_revision": frozen.image_revision,
                    "artifacts": holdout_artifacts,
                    "base_harness": {
                        "sha256": base_harness_artifact.digest,
                        "storage_key": base_harness_artifact.storage_key,
                        "size_bytes": base_harness_artifact.size_bytes,
                        "media_type": base_harness_artifact.media_type,
                    },
                }
                if isinstance(candidate, dict):
                    manifest["candidate"] = candidate["component_artifact"]
                    manifest["candidate_digest"] = candidate["candidate_digest"]
                    manifest["candidate_metadata"] = candidate["manifest_artifact"]
                manifest_bytes = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
                result["holdout_manifest"] = {
                    **manifest,
                    "digest": hashlib.sha256(manifest_bytes).hexdigest(),
                }
                exp.baseline_digest = next((r["harness_digest"] for r in adapter.records if r["phase"] == "baseline"), None)
                exp.result = result
                result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
                stored_result = await asyncio.to_thread(LocalArtifactStore(Path(settings.artifact_root)).put_bytes, result_bytes, media_type="application/vnd.harnessa.harness-experiment+json")
                exp.result_digest, exp.result_storage_key, exp.result_size_bytes = stored_result.digest, stored_result.storage_key, stored_result.size_bytes
            await db.commit()

    async def _grade_holdout(
        self, frozen: FrozenExperiment, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Grade only finalized CAS references; never return grader feedback to an agent."""
        manifest = result.get("holdout_manifest")
        manifest_digest = manifest.get("digest") if isinstance(manifest, dict) else None
        if result.get("composed_successful") is not True:
            not_run = {
                "status": "not_run",
                "reason_code": "composed_artifact_unavailable",
                "manifest_digest": manifest_digest,
            }
            result["holdout_evaluation"] = not_run
            persisted = await self._persist_holdout_evaluation(frozen, result)
            if not persisted:
                not_run = {
                    "status": "unknown",
                    "reason_code": "frozen_identity_changed_before_receipt_commit",
                    "manifest_digest": manifest_digest,
                }
                result["holdout_evaluation"] = not_run
            return not_run
        safe: dict[str, Any] = {
            "status": "unavailable",
            "reason_code": "evaluator_not_configured",
            "manifest_digest": manifest_digest,
        }
        if self.evaluator is not None and isinstance(manifest, dict) and isinstance(manifest_digest, str):
            try:
                async with asyncio.timeout(settings.harness2_holdout_timeout_seconds):
                    raw = await self.evaluator.evaluate(manifest)
                if isinstance(raw, Mapping):
                    raw_bytes = json.dumps(
                        dict(raw), sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                    if len(raw_bytes) <= 64_000:
                        status = raw.get("status")
                        judge_revision = raw.get("judge_revision")
                        if (
                            status in {"passed", "failed"}
                            and raw.get("manifest_digest") == manifest_digest
                            and isinstance(judge_revision, str) and judge_revision
                        ):
                            stored = await asyncio.to_thread(
                                LocalArtifactStore(Path(settings.artifact_root)).put_bytes,
                                raw_bytes,
                                media_type="application/vnd.harnessa.holdout-evaluation+json",
                            )
                            safe = {
                                "status": status,
                                "manifest_digest": manifest_digest,
                                "judge_revision": judge_revision[:120],
                                "result_digest": stored.digest,
                                "result_size_bytes": stored.size_bytes,
                            }
                        else:
                            safe = {
                                "status": "unknown",
                                "reason_code": "invalid_evaluator_receipt",
                                "manifest_digest": manifest_digest,
                            }
                    else:
                        safe = {
                            "status": "unknown",
                            "reason_code": "evaluator_receipt_too_large",
                            "manifest_digest": manifest_digest,
                        }
                else:
                    safe["status"], safe["reason_code"] = "unknown", "invalid_evaluator_receipt"
            except TimeoutError:
                safe["status"], safe["reason_code"] = "unknown", "evaluator_timeout"
            except Exception as exc:  # noqa: BLE001 - evaluator outage must not bypass the gate
                safe["status"], safe["reason_code"] = "unknown", f"evaluator_error_{type(exc).__name__}"
        candidate = result.get("experience_candidate")
        if safe.get("status") == "passed" and isinstance(candidate, dict):
            candidate_ref = candidate.get("component_artifact")
            candidate_digest = candidate.get("candidate_digest")
            candidate_ref_shape_valid = (
                isinstance(candidate_ref, dict)
                and candidate_ref.get("sha256") == candidate.get("component_digest")
                and isinstance(candidate_ref.get("storage_key"), str)
                and isinstance(candidate.get("manifest_artifact"), dict)
                and isinstance(candidate_digest, str)
                and isinstance(manifest, dict)
                and manifest.get("candidate_digest") == candidate_digest
                and manifest.get("candidate_metadata") == candidate.get("manifest_artifact")
            )
            candidate_ref_valid = False
            if candidate_ref_shape_valid and isinstance(candidate_ref, dict):
                store = LocalArtifactStore(Path(settings.artifact_root))
                candidate_ref_valid = (
                    await asyncio.to_thread(
                        store.verify_reference,
                        candidate_ref["storage_key"],
                        expected_digest=candidate_ref["sha256"],
                        expected_size=candidate_ref["size_bytes"],
                        max_bytes=2_000_000,
                    )
                    == "verified"
                )
                metadata_ref = candidate.get("manifest_artifact")
                if candidate_ref_valid and isinstance(metadata_ref, dict):
                    candidate_ref_valid = (
                        await asyncio.to_thread(
                            store.verify_reference,
                            metadata_ref["storage_key"],
                            expected_digest=metadata_ref["sha256"],
                            expected_size=metadata_ref["size_bytes"],
                            max_bytes=256_000,
                        )
                        == "verified"
                    )
            result["promotion_gates"] = {
                "candidate_digest": candidate_digest if candidate_ref_valid else "",
                "regression": (
                    "passed"
                    if result.get("comparison_successful") is True
                    and result.get("composed_successful") is True
                    else "failed"
                ),
                "security": "passed" if candidate_ref_valid else "failed",
                "cost": (
                    "passed"
                    if isinstance(result.get("cost_gate"), dict)
                    and result["cost_gate"].get("status") == "passed"
                    else "blocked"
                ),
            }
        result["holdout_evaluation"] = safe
        persisted = await self._persist_holdout_evaluation(frozen, result)
        if not persisted:
            safe = {
                "status": "unknown",
                "reason_code": "frozen_identity_changed_before_receipt_commit",
                "manifest_digest": manifest_digest,
            }
            result["holdout_evaluation"] = safe
        return safe

    async def _persist_holdout_evaluation(
        self, frozen: FrozenExperiment, result: dict[str, Any]
    ) -> bool:
        async with self._sessions() as db:
            exp = await db.scalar(select(HarnessExperiment).where(
                HarnessExperiment.id == frozen.experiment_id
            ).with_for_update())
            policy = await db.scalar(select(HarnessPolicy).where(
                HarnessPolicy.project_id == frozen.project_id
            ))
            interview = await db.scalar(select(SpecificationInterview).where(
                SpecificationInterview.task_id == frozen.task_id
            ))
            if (
                exp is None or not _experiment_current(exp, frozen)
                or policy is None or policy.revision != frozen.policy_revision
                or interview is None or interview.readiness_state != "READY"
                or (interview.base_sha, interview.artifact_hash, interview.revision)
                != (frozen.base_sha, frozen.specification_hash, frozen.specification_revision)
            ):
                return False
            exp.result = dict(result)
            result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            stored = await asyncio.to_thread(
                LocalArtifactStore(Path(settings.artifact_root)).put_bytes,
                result_bytes,
                media_type="application/vnd.harnessa.harness-experiment+json",
            )
            exp.result_digest = stored.digest
            exp.result_storage_key = stored.storage_key
            exp.result_size_bytes = stored.size_bytes
            await db.commit()
            return True

    async def _cancelled(self, experiment_id: UUID, attempt: int) -> bool:
        async with self._sessions() as db:
            row = await db.get(HarnessExperiment, experiment_id)
            if row is None or row.status != "running" or row.attempts != attempt or row.cancel_requested_at is not None:
                return True
            policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == row.project_id))
            interview = await db.scalar(select(SpecificationInterview).where(SpecificationInterview.task_id == row.task_id))
            return bool(
                policy is None or policy.revision != row.policy_revision
                or interview is None or interview.readiness_state != "READY"
                or (interview.base_sha, interview.artifact_hash, interview.revision)
                != (row.base_sha, row.specification_hash, row.specification_revision)
            )

    async def _heartbeat(self, experiment_id: UUID, attempt: int) -> None:
        async with self._sessions() as db:
            row = await db.scalar(select(HarnessExperiment).where(
                HarnessExperiment.id == experiment_id
            ).with_for_update())
            if row and row.status == "running" and row.attempts == attempt:
                row.worker_heartbeat_at = datetime.now(UTC)
                await db.commit()

    async def _heartbeat_loop(self, experiment_id: UUID, attempt: int) -> None:
        while True:
            await asyncio.sleep(10)
            if await self._cancelled(experiment_id, attempt):
                return
            await self._heartbeat(experiment_id, attempt)

    async def is_current_fence(self, run_id: UUID, fencing_token: int) -> bool:
        async with self._sessions() as db:
            run = await db.get(Run, run_id)
            if (run is None or run.adapter_name not in {"harness2-probe", "harness2-proposer"}
                    or run.fencing_token != fencing_token or run.status != "RUNNING"):
                return False
            raw_experiment_id = run.execution_result.get("experiment_id") if run.execution_result else None
            try:
                experiment_id = UUID(raw_experiment_id) if isinstance(raw_experiment_id, str) else None
            except ValueError:
                experiment_id = None
            experiment = await db.get(HarnessExperiment, experiment_id) if experiment_id else None
            policy = await db.scalar(select(HarnessPolicy).where(HarnessPolicy.project_id == run.project_id))
            return bool(
                experiment and policy and experiment.task_id == run.task_id
                and experiment.status == "running" and experiment.attempts == fencing_token
                and experiment.cancel_requested_at is None and policy.revision == experiment.policy_revision
            )

    async def _finish(self, experiment_id: UUID, attempt: int, status: str, error: str | None) -> None:
        async with self._sessions() as db:
            row = await db.scalar(select(HarnessExperiment).where(HarnessExperiment.id == experiment_id).with_for_update())
            if row is None or row.status != "running" or row.attempts != attempt:
                return
            if status != "cancelled" and row.cancel_requested_at is not None:
                status = "cancelled"
                error = "Cancellation was accepted before experiment finalization"
                result = dict(row.result or {})
                result["status"] = "cancelled"
                result["composed_successful"] = False
                result["cancellation_reason"] = error
                result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
                stored = await asyncio.to_thread(
                    LocalArtifactStore(Path(settings.artifact_root)).put_bytes,
                    result_bytes,
                    media_type="application/vnd.harnessa.harness-experiment+json",
                )
                row.result = result
                row.result_digest = stored.digest
                row.result_storage_key = stored.storage_key
                row.result_size_bytes = stored.size_bytes
            row.status, row.phase, row.last_error = status, "complete" if status == "succeeded" else status, error
            row.completed_at = datetime.now(UTC)
            proposer_run_id = uuid5(_RUN_NAMESPACE, f"{experiment_id}:{attempt}:proposer")
            proposer_run = await db.get(Run, proposer_run_id)
            if proposer_run and proposer_run.status == "RUNNING":
                proposer_run.status = (
                    "SUCCEEDED" if status == "succeeded"
                    else "CANCELLED" if status == "cancelled"
                    else "FAILED"
                )
                proposer_run.finished_at = datetime.now(UTC)
                if error:
                    proposer_run.error_code = status[:80]
                    proposer_run.error_message = error[:300]
                await db.flush()
            rollout_runs = list(await db.scalars(select(Run).where(
                Run.task_id == row.task_id,
                Run.fencing_token == attempt,
                Run.adapter_name == "harness2-probe",
                Run.status == "RUNNING",
            ).with_for_update()))
            for rollout in rollout_runs:
                rollout.status = "CANCELLED" if status == "cancelled" else "FAILED"
                rollout.finished_at = datetime.now(UTC)
                rollout.error_code = status[:80]
                rollout.error_message = (error or "Experiment ended before rollout completion")[:300]
            await db.commit()


def _experiment_current(row: HarnessExperiment, frozen: FrozenExperiment) -> bool:
    return row.id == frozen.experiment_id and row.status == "running" and row.attempts == frozen.claim_attempt and row.cancel_requested_at is None


def _flatten_harness(root: Path) -> str:
    tree = harness_tree.read_tree(_SURFACE, root)
    validate_product_tree(tree)
    return "\n\n".join(f"## {path}\n{body}" for path, body in sorted(tree.items()))[:128_000]


def _probe_label(value: str) -> str:
    suffix = value[-64:]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{suffix}-{digest}"[:100]


def _matched_comparison(
    result: Mapping[str, Any], records: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    """Require a delivered baseline and delivered final candidates in each mode."""
    issues: list[str] = []
    if result.get("comparison_successful") is not True:
        issues.append("upstream_comparison_failed")
    baselines = [row for row in records if row.get("phase") == "baseline"]
    if (
        len(baselines) != 1
        or baselines[0].get("status") != "succeeded"
        or not baselines[0].get("delivered")
    ):
        issues.append("baseline_rollout_missing_or_failed")
    modes = result.get("modes")
    if not isinstance(modes, Mapping) or not modes:
        issues.append("mode_results_missing")
    else:
        for mode_result in modes.values():
            if not isinstance(mode_result, Mapping) or mode_result.get("ok") is not True:
                issues.append("mode_failed")
                continue
            finals = mode_result.get("finals")
            if not isinstance(finals, list) or not finals:
                issues.append("final_candidates_missing")
                continue
            for label in finals:
                if not isinstance(label, str):
                    issues.append("final_candidate_label_invalid")
                    continue
                candidate = next(
                    (
                        row for row in records
                        if row.get("phase") == "candidate"
                        and str(row.get("source_run_id", "")).rsplit("/", 1)[-1] == label
                    ),
                    None,
                )
                if (
                    candidate is None
                    or candidate.get("status") != "succeeded"
                    or not candidate.get("delivered")
                ):
                    issues.append("final_candidate_rollout_missing_or_failed")
    return not issues, sorted(set(issues))


def _assert_holdout_free_tree(source: Path, revision: str) -> None:
    """Reject repository trees that would expose hidden evaluation material to ACP."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-tree", "-r", "--name-only", "-z", revision],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessAdaptationError("Could not verify the frozen repository evaluation boundary") from exc
    paths = result.stdout.decode("utf-8", errors="replace").split("\0")
    if any(path and _is_holdout_path(Path(path)) for path in paths):
        raise HarnessAdaptationError("Frozen repository contains restricted evaluation material")
