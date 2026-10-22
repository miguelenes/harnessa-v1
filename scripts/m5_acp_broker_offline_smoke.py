"""Verify the sandbox-to-Unix-socket model broker path without a provider call."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from harnessa.adapters.acp_execution import _opencode_model_config
from harnessa.adapters.acp_model_broker import ACPModelBroker, BrokerScope
from harnessa.adapters.acp_session import ACPAgentSession
from harnessa.adapters.agent_registry import get_agent
from harnessa.adapters.execution import RunInput, TaskInput
from harnessa.adapters.podman_sandbox import PodmanSandboxBackend, SandboxIdentity
from harnessa.adapters.sandbox_policy import SandboxLimits
from harnessa.adapters.workspaces import WorkspaceManager
from harnessa.contracts import ModelRouteRequest

IMAGE = (
    "ghcr.io/anomalyco/opencode:1.19.4@sha256:"
    "bf8e2b5f785166a12a26cbb34ef1a2d35403368438efe036fd16c795a975c505"
)
TOKEN = "offline-run-scoped-token"


class _Session(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _MockModelRouter:
    def __init__(self) -> None:
        self.requests: list[ModelRouteRequest] = []
        self.scope: tuple[UUID, UUID] | None = None
        self.scopes: list[tuple[UUID, UUID]] = []

    async def complete(
        self, db: Any, *, organization_id, project_id, request, idempotency_key
    ) -> dict[str, Any]:
        del db, idempotency_key
        self.scope = organization_id, project_id
        self.scopes.append(self.scope)
        self.requests.append(request)
        return {
            "response": {
                "id": "offline-model-response",
                "object": "chat.completion",
                "model": "fixture-route",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "offline-ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        }

    async def stream(
        self,
        db: Any,
        *,
        organization_id: UUID,
        project_id: UUID,
        request: ModelRouteRequest,
        idempotency_key: str,
    ) -> tuple[UUID, AsyncIterator[bytes]]:
        del db, idempotency_key
        self.scope = organization_id, project_id
        self.scopes.append(self.scope)
        self.requests.append(request)

        async def chunks() -> AsyncIterator[bytes]:
            yield (
                b'data: {"id":"offline-model-response","object":"chat.completion.chunk",'
                b'"created":0,"model":"fixture-route","choices":[{"index":0,'
                b'"delta":{"role":"assistant","content":"offline-ok"},'
                b'"finish_reason":null}]}\n\n'
            )
            yield (
                b'data: {"id":"offline-model-response","object":"chat.completion.chunk",'
                b'"created":0,"model":"fixture-route","choices":[{"index":0,'
                b'"delta":{},"finish_reason":"stop"}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

        return uuid4(), chunks()


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="harnessa-m5-broker-smoke-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        _run("git", "init", "--quiet", str(source))
        _run("git", "-C", str(source), "config", "user.name", "Harnessa M5 smoke")
        _run("git", "-C", str(source), "config", "user.email", "smoke@example.invalid")
        (source / "README.md").write_text("committed base\n", encoding="utf-8")
        _run("git", "-C", str(source), "add", "README.md")
        _run("git", "-C", str(source), "commit", "--quiet", "-m", "base")
        source_commit = _run("git", "-C", str(source), "rev-parse", "HEAD")

        task = TaskInput(uuid4(), uuid4(), "Broker fixture", "Offline route only")
        run = RunInput(uuid4(), attempt=1, fencing_token=3)
        workspace_manager = WorkspaceManager(root / "managed-workspaces")
        workspace = await workspace_manager.provision(task, run, source)
        state_root = root / "runner-state"
        state_root.mkdir(mode=0o700)
        host_secret = state_root / "host-secret.txt"
        host_secret.write_text("synthetic fixture secret", encoding="utf-8")
        other_workspace = root / "other-task-workspace"
        other_workspace.mkdir(mode=0o700)
        (other_workspace / "sentinel").write_text("other task", encoding="utf-8")
        backend = PodmanSandboxBackend(
            image=IMAGE,
            owner_id=str(uuid4()),
            limits=SandboxLimits(),
        )
        identity = SandboxIdentity(
            owner_id=backend.owner_id,
            run_id=str(run.id),
            task_id=str(task.id),
            attempt=run.attempt,
            fencing_token=run.fencing_token,
        )
        broker_root = state_root / "broker"
        broker_root.mkdir(mode=0o700)
        config_path = broker_root / "opencode.json"
        config_path.write_text(json.dumps(_opencode_model_config(TOKEN, 128)), encoding="utf-8")
        config_path.chmod(0o600)
        router = _MockModelRouter()
        organization_id = uuid4()
        broker = ACPModelBroker(
            socket_path=broker_root / "model.sock",
            scope=BrokerScope(
                token=TOKEN,
                organization_id=organization_id,
                project_id=task.project_id,
                task_id=task.id,
                run_id=run.id,
                fencing_token=run.fencing_token,
                model_alias="offline-fixture",
            ),
            model_router=router,
            sessions=_Session,
            is_current_fence=lambda run_id, token: _resolved(
                run_id == run.id and token == run.fencing_token
            ),
            is_cancelled=lambda: _resolved(False),
            max_output_tokens=128,
        )
        await broker.start()

        process: asyncio.subprocess.Process | None = None
        session: ACPAgentSession | None = None
        try:
            await backend.validate()
            relay = await backend.build_model_relay(state_root / "model-relay")
            agent = get_agent("opencode")
            if agent.command is None:
                raise RuntimeError("OpenCode ACP command is not configured")
            process = await backend.launch(
                identity=identity,
                workspace=workspace.workspace_path,
                state_root=state_root,
                agent_command=agent.command[1:],
                max_frame_bytes=1024 * 1024,
                broker_socket=broker.socket_path,
                opencode_config=config_path,
                relay_binary=relay,
            )
            session = ACPAgentSession(
                process,
                max_frame_bytes=1024 * 1024,
                max_updates=10_000,
                workspace_root=workspace.workspace_path,
            )
            initialized = await session.initialize()
            await session.new_session()
            prompt = "Reply with exactly offline-ok. Do not call any tools."
            stop_reason = await session.prompt(prompt, timeout_seconds=30)
            if stop_reason != "end_turn":
                raise RuntimeError(f"offline ACP prompt ended unexpectedly: {stop_reason}")
            container = json.loads(_run("podman", "inspect", identity.container_name))[0]
            mounts = container.get("Mounts", [])
            destinations = {mount.get("Destination") for mount in mounts}
            engine_socket = _run("podman", "info", "--format", "{{.Host.RemoteSocket.Path}}")
            forbidden_paths = [
                str(Path.cwd().resolve()),
                str(host_secret.resolve()),
                str((other_workspace / "sentinel").resolve()),
                "/var/run/docker.sock",
                engine_socket,
            ]
            for forbidden_path in forbidden_paths:
                inaccessible = await asyncio.create_subprocess_exec(
                    "podman",
                    "exec",
                    identity.container_name,
                    "busybox",
                    "test",
                    "!",
                    "-e",
                    forbidden_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await inaccessible.communicate()
                assert inaccessible.returncode == 0, f"sandbox can access {forbidden_path}"
            assert initialized.protocol_version == 1
            assert initialized.agent_info is not None
            assert initialized.agent_info.version in agent.supported_versions
            assert router.scope == (organization_id, task.project_id)
            # This ACP release may make a setup stream before its prompt stream.
            # Bound both and verify every request remains scoped and token-capped.
            assert 1 <= len(router.requests) <= 2
            assert router.scopes == [(organization_id, task.project_id)] * len(router.requests)
            for routed in router.requests:
                assert routed.task_id == task.id and routed.run_id == run.id
                assert routed.model_alias == "offline-fixture"
                assert 1 <= routed.max_output_tokens <= 128
                assert routed.stream
                assert routed.messages and routed.messages[-1].content == prompt
            routed = router.requests[-1]
            assert workspace.base_commit == source_commit
            assert destinations == {
                "/workspace",
                "/run/harnessa/model.sock",
                "/run/harnessa/opencode.json",
                "/harnessa/model-relay",
            }
            assert container.get("HostConfig", {}).get("NetworkMode") == "none"
            assert all(
                mount.get("RW") is False
                for mount in mounts
                if mount.get("Destination") != "/workspace"
            )
            print(
                json.dumps(
                    {
                        "agent_version": initialized.agent_info.version,
                        "protocol_version": initialized.protocol_version,
                        "base_commit": workspace.base_commit,
                        "model_route": "in-process fake ModelRouter; no provider call",
                        "task_run_fence_bound": True,
                        "container_network": "none",
                        "mount_destinations": sorted(destinations),
                        "forbidden_host_paths_checked": len(forbidden_paths),
                        "model_response": "offline-ok",
                        "max_output_tokens": routed.max_output_tokens,
                        "fake_router_request_count": len(router.requests),
                        "prompt_sent_to_agent": True,
                        "prompt_stop_reason": stop_reason,
                        "provider_prompt_sent": False,
                        "provider_call": False,
                    },
                    sort_keys=True,
                )
            )
        finally:
            if session is not None:
                await session.close()
            elif process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            await broker.close()
            await backend.stop_and_remove(identity)
            await workspace_manager.remove_workspace(workspace)


async def _resolved(value: bool) -> bool:
    return value


if __name__ == "__main__":
    asyncio.run(main())
