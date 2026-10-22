"""Fenced ACP execution over isolated rootless Podman workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harnessa.adapters.acp_session import ACPAgentSession, ACPProtocolError
from harnessa.adapters.acp_model_broker import ACPModelBroker, BrokerScope
from harnessa.adapters.agent_registry import get_agent
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.execution import (
    ExecutionFailure,
    RunInput,
    StaleLease,
    TaskInput,
)
from harnessa.adapters.podman_sandbox import (
    PodmanSandboxBackend,
    SandboxBackend,
    SandboxIdentity,
    SandboxUnavailable,
)
from harnessa.adapters.sandbox_policy import SandboxLimits
from harnessa.adapters.workspaces import ManagedWorkspace, WorkspaceError, WorkspaceManager
from harnessa.adapters.protocols import ModelRouter
from harnessa.db.models import Project
from harnessa.db.session import SessionLocal
from harnessa.services.model_gateway import LiteLLMModelRouter

logger = logging.getLogger("harnessa.acp_execution")
ArtifactPublisher = Callable[[bytes, str], Awaitable[Mapping[str, Any]]]


class ACPExecutionAdapter:
    """An ACP client adapter; the existing Scheduler remains checkpoint authority."""

    name = "acp-opencode"

    def __init__(
        self,
        *,
        enabled: bool,
        repository_sources: Mapping[str, Path],
        workspace_root: Path,
        state_root: Path,
        artifact_store: LocalArtifactStore,
        image: str,
        owner_id: str | None = None,
        limits: SandboxLimits | None = None,
        expected_agent_version: str | None = "2.0.32",
        model_route_ready: bool = False,
        model_router: ModelRouter | None = None,
        sessions_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        model_alias: str | None = None,
        model_max_output_tokens: int = 8192,
    ) -> None:
        self.enabled = enabled
        self.model_route_ready = model_route_ready
        self.model_router = model_router or (LiteLLMModelRouter() if model_route_ready else None)
        self.sessions_factory = sessions_factory
        self.model_alias = model_alias
        self.model_max_output_tokens = model_max_output_tokens
        self.repository_sources = {key: Path(value) for key, value in repository_sources.items()}
        self.limits = limits or SandboxLimits()
        self.workspaces = WorkspaceManager(
            workspace_root, size_limit_bytes=self.limits.workspace_bytes
        )
        self.state_root = state_root.expanduser().resolve()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self.agent = get_agent("opencode")
        self.expected_agent_version = expected_agent_version
        self.owner_id = owner_id or _load_owner_id(self.state_root)
        self.backend: SandboxBackend = PodmanSandboxBackend(
            image=image,
            owner_id=self.owner_id,
            limits=self.limits,
        )
        self._active: dict[
            UUID, tuple[ACPAgentSession | None, SandboxIdentity, asyncio.subprocess.Process | None]
        ] = {}
        self._active_lock = asyncio.Lock()
        self._validated_at = 0.0
        self._validated = False

    @property
    def available(self) -> bool:
        # Only a broker that spends through Harnessa's ModelRouter can enable dispatch.
        return (
            self.enabled
            and self.model_route_ready
            and self.model_router is not None
            and bool(self.repository_sources)
            and bool(
                shutil.which(self.backend.executable) or Path(self.backend.executable).is_file()
            )
            and self._validated
        )

    @property
    def sandbox_validated(self) -> bool:
        return self._validated

    async def validate(self) -> None:
        try:
            await self.backend.validate()
        except Exception:
            self._validated = False
            raise
        self._validated = True
        self._validated_at = time.monotonic()

    async def execute(
        self,
        task: TaskInput,
        run: RunInput,
        *,
        checkpoint: Mapping[str, Any] | None,
        save_checkpoint: Callable[[Mapping[str, Any]], Awaitable[None]],
        is_cancelled: Callable[[], Awaitable[bool]],
        is_current_fence: Callable[[UUID, int], Awaitable[bool]],
        publish_artifact: ArtifactPublisher,
    ) -> Mapping[str, Any]:
        if not self.model_route_ready or self.model_router is None:
            raise ExecutionFailure(
                "permanent",
                "model_route_unavailable",
                "ACP model calls require the run-scoped Harnessa ModelRouter broker",
            )
        recovery_context = _safe_recovery_context(checkpoint, task)
        repository = self.repository_sources.get(str(task.project_id))
        if repository is None:
            raise ExecutionFailure(
                "permanent",
                "repository_source_not_configured",
                "No managed repository source is configured",
            )
        if not self._validated or time.monotonic() - self._validated_at > 60:
            await self.validate()

        workspace: ManagedWorkspace | None = None
        session: ACPAgentSession | None = None
        broker: ACPModelBroker | None = None
        process: asyncio.subprocess.Process | None = None
        monitor_task: asyncio.Task[None] | None = None
        workspace_limit_hit = asyncio.Event()
        identity = SandboxIdentity(
            owner_id=self.owner_id,
            run_id=str(run.id),
            task_id=str(task.id),
            attempt=run.attempt,
            fencing_token=run.fencing_token,
        )
        successful = False
        started = datetime.now(timezone.utc)
        deadline = started.timestamp() + self.limits.wall_seconds
        broker_root = self.state_root / "brokers" / identity.container_name
        config_path = broker_root / "opencode.json"
        try:
            workspace = await self.workspaces.provision(
                task,
                run,
                repository,
                requested_base_commit=recovery_context.get("base_commit"),
            )
            await self.workspaces.mark_status(workspace, "running")
            await save_checkpoint(
                self._checkpoint(
                    workspace,
                    identity,
                    "session_starting",
                    started,
                    deadline,
                    recovery_context=recovery_context,
                )
            )
            if await is_cancelled():
                return {
                    "status": "cancelled",
                    "workspace": self._workspace_summary(workspace, identity, deadline),
                }

            if self.agent.command is None:
                raise ExecutionFailure(
                    "permanent", "agent_command_missing", "ACP agent command is unavailable"
                )

            async with self.sessions_factory() as db:
                project = await db.get(Project, task.project_id)
                if project is None:
                    raise ExecutionFailure(
                        "permanent", "project_not_found", "Run project is no longer available"
                    )
                organization_id = project.organization_id
            broker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            token = secrets.token_urlsafe(32)
            broker = ACPModelBroker(
                socket_path=broker_root / "model.sock",
                scope=BrokerScope(
                    token=token,
                    organization_id=organization_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    run_id=run.id,
                    fencing_token=run.fencing_token,
                    model_alias=self.model_alias,
                ),
                model_router=self.model_router,
                sessions=self.sessions_factory,
                is_current_fence=is_current_fence,
                is_cancelled=is_cancelled,
                max_output_tokens=self.model_max_output_tokens,
            )
            await broker.start()
            config_path.write_text(
                json.dumps(_opencode_model_config(token, self.model_max_output_tokens)),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            relay_binary = await self.backend.build_model_relay(
                self.state_root / "acp-model-relay"
            )

            process = await self.backend.launch(
                identity=identity,
                workspace=workspace.workspace_path,
                state_root=self.state_root,
                agent_command=self.agent.command[1:],
                max_frame_bytes=self.limits.max_protocol_frame_bytes,
                broker_socket=broker.socket_path,
                opencode_config=config_path,
                relay_binary=relay_binary,
            )
            monitor_task = asyncio.create_task(
                self._workspace_watchdog(workspace, process, workspace_limit_hit)
            )

            async def checkpoint_progress(counts: Mapping[str, int]) -> None:
                await save_checkpoint(
                    self._checkpoint(
                        workspace,
                        identity,
                        "session_update",
                        started,
                        deadline,
                        progress_counts=counts,
                        recovery_context=recovery_context,
                    )
                )

            session = ACPAgentSession(
                process,
                max_frame_bytes=self.limits.max_protocol_frame_bytes,
                max_updates=self.limits.max_agent_updates,
                on_progress=checkpoint_progress,
                workspace_root=workspace.workspace_path,
            )
            async with self._active_lock:
                self._active[run.id] = (session, identity, process)

            initialized = await session.initialize()
            capability_data = (
                initialized.agent_capabilities.model_dump(mode="json")
                if initialized.agent_capabilities
                else {}
            )
            agent_session_resume_supported = bool(
                initialized.agent_capabilities
                and initialized.agent_capabilities.session_capabilities
                and initialized.agent_capabilities.session_capabilities.resume is not None
            )
            agent_info = (
                initialized.agent_info.model_dump(mode="json") if initialized.agent_info else {}
            )
            observed_version = str(agent_info.get("version", ""))
            if (
                self.expected_agent_version
                and observed_version
                and observed_version != self.expected_agent_version
            ):
                raise ACPProtocolError(
                    "ACP agent version differs from the pinned compatibility record"
                )
            await session.new_session()
            await save_checkpoint(
                self._checkpoint(
                    workspace,
                    identity,
                    "prompt_running",
                    started,
                    deadline,
                    capabilities=capability_data,
                    agent_info=agent_info,
                    recovery_context=recovery_context,
                    agent_session_resume_supported=agent_session_resume_supported,
                )
            )
            if await is_cancelled():
                return {
                    "status": "cancelled",
                    "workspace": self._workspace_summary(workspace, identity, deadline),
                }

            prompt = _task_prompt(task, workspace.base_commit, recovery_context)
            remaining = max(0.1, deadline - datetime.now(timezone.utc).timestamp())
            stop_reason = await session.prompt(prompt, timeout_seconds=remaining)
            if workspace_limit_hit.is_set():
                raise WorkspaceError("The workspace exceeded its configured monitored size limit")
            if stop_reason == "cancelled" or await is_cancelled():
                return {
                    "status": "cancelled",
                    "workspace": self._workspace_summary(workspace, identity, deadline),
                }
            if stop_reason != "end_turn":
                raise ExecutionFailure(
                    "permanent", "acp_prompt_not_completed", "ACP agent did not complete the turn"
                )

            files = await self.workspaces.changed_files(workspace)
            patch = await self.workspaces.patch(workspace, self.limits.max_artifact_bytes)
            artifact: Mapping[str, Any] | None = None
            if patch:
                artifact = await publish_artifact(patch, "application/vnd.git-patch")
            if await is_cancelled():
                return {
                    "status": "cancelled",
                    "workspace": self._workspace_summary(workspace, identity, deadline),
                }
            successful = True
            result = {
                "status": "completed",
                "agent_id": self.agent.agent_id,
                "agent_version": observed_version or self.expected_agent_version,
                "protocol_version": initialized.protocol_version,
                "resume_supported": bool(
                    initialized.agent_capabilities
                    and initialized.agent_capabilities.session_capabilities
                    and initialized.agent_capabilities.session_capabilities.resume
                ),
                "resumed": False,
                "session_update_counts": dict(session.client.update_counts),
                "changed_files": files,
                "patch_sha256": hashlib.sha256(patch).hexdigest() if patch else None,
                "patch_artifact": dict(artifact) if artifact else None,
                "workspace": self._workspace_summary(workspace, identity, deadline),
            }
            await self.workspaces.mark_status(workspace, "completed")
            return result
        except StaleLease:
            raise
        except asyncio.CancelledError:
            await self.cancel(run.id)
            raise
        except TimeoutError as exc:
            raise ExecutionFailure(
                "transient", "acp_timeout", "ACP execution exceeded a time bound"
            ) from exc
        except (ACPProtocolError, WorkspaceError, SandboxUnavailable) as exc:
            raise ExecutionFailure("permanent", "acp_execution_rejected", str(exc)[:300]) from exc
        finally:
            if broker is not None:
                await broker.close()
            shutil.rmtree(broker_root, ignore_errors=True)
            if monitor_task is not None:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
            async with self._active_lock:
                self._active.pop(run.id, None)
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    logger.warning("ACP session cleanup failed for run %s", run.id)
            elif process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            try:
                await self.backend.stop_and_remove(identity)
            except Exception:
                logger.warning("Could not clean up owned ACP container for run %s", run.id)
            if workspace is not None:
                if successful:
                    try:
                        await self.workspaces.remove_workspace(workspace)
                    except OSError:
                        logger.warning(
                            "Could not remove completed ACP workspace for run %s", run.id
                        )
                else:
                    try:
                        await self.workspaces.mark_status(workspace, "preserved_for_recovery")
                    except OSError:
                        pass

    async def _workspace_watchdog(
        self,
        workspace: ManagedWorkspace,
        process: asyncio.subprocess.Process,
        limit_hit: asyncio.Event,
    ) -> None:
        while process.returncode is None:
            if await self.workspaces.size_bytes(workspace) > self.limits.workspace_bytes:
                limit_hit.set()
                process.terminate()
                return
            await asyncio.sleep(1)

    async def cancel(self, run_id: UUID) -> None:
        async with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            return
        session, identity, process = active
        if session is not None:
            await session.cancel()
        try:
            await self.backend.stop_and_remove(identity)
        except Exception:
            pass
        if process is not None and process.returncode is None:
            process.terminate()

    async def reconcile_orphans(
        self,
        *,
        is_current_fence: Callable[[UUID, int], Awaitable[bool]],
    ) -> int:
        removed = 0
        for row in await self.backend.owned_containers():
            labels = row.get("Labels") or row.get("labels") or {}
            if isinstance(labels, str):
                parsed: dict[str, str] = {}
                for item in labels.split(","):
                    key, separator, value = item.partition("=")
                    if separator:
                        parsed[key] = value
                labels = parsed
            if not isinstance(labels, dict) or labels.get("harnessa.owner") != self.owner_id:
                continue
            try:
                run_id = UUID(str(labels["harnessa.run_id"]))
                fence = int(labels["harnessa.fencing_token"])
            except (KeyError, ValueError):
                continue
            if await is_current_fence(run_id, fence):
                continue
            container_id = str(row.get("Id") or row.get("ID") or "")
            await self.backend.remove_owned(container_id)
            removed += 1
        return removed

    def _checkpoint(
        self,
        workspace: ManagedWorkspace,
        identity: SandboxIdentity,
        phase: str,
        started: datetime,
        deadline: float,
        *,
        capabilities: Mapping[str, Any] | None = None,
        agent_info: Mapping[str, Any] | None = None,
        progress_counts: Mapping[str, int] | None = None,
        recovery_context: Mapping[str, Any] | None = None,
        agent_session_resume_supported: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": "acp_execution",
            "phase": phase,
            # This Harnessa checkpoint can seed a fresh attempt independently of the
            # ACP session resume capability reported in the next field.
            "resumable": True,
            "agent_session_resume_supported": agent_session_resume_supported,
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "attempt": identity.attempt,
            "fencing_token": identity.fencing_token,
            "base_commit": workspace.base_commit,
            "branch": workspace.branch,
            "image_digest": self.backend.image.rsplit("@", 1)[1],
            "started_at": started.isoformat(),
            "deadline_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
            "resource_limits": _limits_dict(self.limits),
            "egress": "none",
            "capabilities": dict(capabilities or {}),
            "agent_info": _safe_agent_info(agent_info or {}),
            "update_counts": dict(progress_counts or {}),
            "recovery_context": dict(recovery_context or {}),
        }

    def _workspace_summary(
        self,
        workspace: ManagedWorkspace,
        identity: SandboxIdentity,
        deadline: float,
    ) -> dict[str, Any]:
        return {
            "owner_id": identity.owner_id,
            "task_id": identity.task_id,
            "project_id": str(workspace.project_id),
            "run_id": identity.run_id,
            "attempt": identity.attempt,
            "fencing_token": identity.fencing_token,
            "base_commit": workspace.base_commit,
            "branch": workspace.branch,
            "image": self.backend.image,
            "mounts": [{"source": "managed_run_workspace", "target": "/workspace", "mode": "rw"}],
            "egress_grants": [],
            "resource_limits": _limits_dict(self.limits),
            "deadline_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
            "cleanup_policy": "remove_success_preserve_failure_until_operator_recovery_or_retention",
        }


def _load_owner_id(state_root: Path) -> str:
    path = state_root / "installation-id"
    try:
        value = path.read_text(encoding="ascii").strip()
        UUID(value)
        return value
    except (OSError, ValueError):
        value = str(uuid4())
        path.write_text(value, encoding="ascii")
        os.chmod(path, 0o600)
        return value


def _task_prompt(
    task: TaskInput,
    base_commit: str,
    recovery_context: Mapping[str, Any] | None = None,
) -> str:
    title = task.title[:300]
    description = task.description[:20000]
    prompt = (
        "Implement the requested task in the isolated repository workspace. "
        "Only read or modify files under /workspace. Do not access network services, "
        "credentials, or container engines. Do not report private reasoning.\n\n"
        f"Task: {title}\n\nDescription:\n{description}\n\n"
        f"Exact base commit: {base_commit}"
    )
    if recovery_context:
        prompt += (
            "\n\nThis is a fresh ACP session for a new Harnessa attempt. The previous "
            "agent session was not resumed; its working directory and conversation are "
            "not available. Continue from the same exact base commit using the task "
            "description and these redacted checkpoint facts only: "
            f"{json.dumps(dict(recovery_context), sort_keys=True)}"
        )
    return prompt


def _safe_recovery_context(
    checkpoint: Mapping[str, Any] | None,
    task: TaskInput,
) -> dict[str, Any]:
    if (
        not checkpoint
        or checkpoint.get("kind") != "acp_execution"
        or checkpoint.get("task_id") != str(task.id)
    ):
        return {}
    context: dict[str, Any] = {}
    previous_run = checkpoint.get("run_id")
    try:
        if isinstance(previous_run, str):
            context["run_id"] = str(UUID(previous_run))
    except ValueError:
        pass
    base_commit = checkpoint.get("base_commit")
    if (
        isinstance(base_commit, str)
        and len(base_commit) in {40, 64}
        and all(character in "0123456789abcdef" for character in base_commit)
    ):
        context["base_commit"] = base_commit
    phase = checkpoint.get("phase")
    if isinstance(phase, str) and phase in {
        "session_starting",
        "session_update",
        "prompt_running",
    }:
        context["phase"] = phase
    attempt = checkpoint.get("attempt")
    if isinstance(attempt, int) and attempt > 0:
        context["attempt"] = attempt
    resume_supported = checkpoint.get("agent_session_resume_supported")
    if isinstance(resume_supported, bool):
        context["agent_session_resume_supported"] = resume_supported
    counts = checkpoint.get("update_counts")
    if isinstance(counts, Mapping):
        context["update_counts"] = {
            key[:80]: value
            for key, value in list(counts.items())[:32]
            if isinstance(key, str) and isinstance(value, int) and 0 <= value <= 10_000
        }
    return context


def _limits_dict(limits: SandboxLimits) -> dict[str, Any]:
    return {
        "cpus": limits.cpus,
        "memory_bytes": limits.memory_bytes,
        "pids": limits.pids,
        "tmpfs_bytes": limits.tmpfs_bytes,
        "workspace_bytes_monitor_threshold": limits.workspace_bytes,
        "wall_seconds": limits.wall_seconds,
        "max_protocol_frame_bytes": limits.max_protocol_frame_bytes,
        "max_agent_updates": limits.max_agent_updates,
        "max_artifact_bytes": limits.max_artifact_bytes,
    }


def _opencode_model_config(token: str, max_output_tokens: int) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": "harnessa/default",
        "autoupdate": False,
        "provider": {
            "harnessa": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Harnessa budget broker",
                "options": {
                    "baseURL": "http://127.0.0.1:8765/v1",
                    "apiKey": token,
                },
                "models": {
                    "default": {
                        "name": "Harnessa scoped model route",
                        "limit": {
                            "context": 2_000_000,
                            "output": max_output_tokens,
                        },
                    }
                },
            }
        },
    }


def _safe_agent_info(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: str(value[key])[:120]
        for key in ("name", "title", "version")
        if value.get(key) is not None
    }
