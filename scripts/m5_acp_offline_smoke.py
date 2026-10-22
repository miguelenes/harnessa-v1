"""Exercise the pinned OpenCode ACP handshake in an isolated offline Podman run."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from harnessa.adapters.acp_session import ACPAgentSession
from harnessa.adapters.agent_registry import get_agent
from harnessa.adapters.execution import RunInput, TaskInput
from harnessa.adapters.podman_sandbox import PodmanSandboxBackend, SandboxIdentity
from harnessa.adapters.sandbox_policy import SandboxLimits
from harnessa.adapters.workspaces import WorkspaceManager

IMAGE = (
    "ghcr.io/anomalyco/opencode:1.19.4@sha256:"
    "bf8e2b5f785166a12a26cbb34ef1a2d35403368438efe036fd16c795a975c505"
)


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def _exists_in_container(name: str, *path: str) -> bool:
    return subprocess.run(
        ["podman", "exec", name, "test", *path],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="harnessa-m5-smoke-") as temporary:
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

        other_workspace = root / "other-task-workspace"
        other_workspace.mkdir()
        (other_workspace / "other-task-secret").write_text("not mounted", encoding="utf-8")
        (root / "host-secret").write_text("not mounted", encoding="utf-8")

        task = TaskInput(uuid4(), uuid4(), "Offline ACP smoke", "Handshake only")
        run = RunInput(uuid4(), attempt=1, fencing_token=1)
        workspace_manager = WorkspaceManager(root / "managed-workspaces")
        workspace = await workspace_manager.provision(task, run, source)
        state_root = root / "runner-state"
        state_root.mkdir(mode=0o700)
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
        process: asyncio.subprocess.Process | None = None
        session: ACPAgentSession | None = None
        try:
            await backend.validate()
            agent = get_agent("opencode")
            if agent.command is None:
                raise RuntimeError("OpenCode ACP command is not configured")
            process = await backend.launch(
                identity=identity,
                workspace=workspace.workspace_path,
                state_root=state_root,
                agent_command=agent.command[1:],
                max_frame_bytes=1024 * 1024,
            )
            session = ACPAgentSession(
                process,
                max_frame_bytes=1024 * 1024,
                max_updates=10_000,
                workspace_root=workspace.workspace_path,
            )
            initialized = await session.initialize()
            session_id = await session.new_session()
            capabilities = (
                initialized.agent_capabilities.model_dump(mode="json")
                if initialized.agent_capabilities
                else {}
            )
            container = json.loads(_run("podman", "inspect", identity.container_name))[0]
            mounts = container.get("Mounts", [])
            env_names = {
                item.split("=", 1)[0]
                for item in container.get("Config", {}).get("Env", [])
                if isinstance(item, str)
            }
            isolation = {
                "source_checkout_git_visible": _exists_in_container(
                    identity.container_name, "-e", str(source / ".git")
                ),
                "other_workspace_visible": _exists_in_container(
                    identity.container_name, "-e", "/other-task-workspace/other-task-secret"
                ),
                "host_secret_visible": _exists_in_container(
                    identity.container_name, "-e", "/host-secret"
                ),
                "podman_socket_visible": _exists_in_container(
                    identity.container_name, "-S", "/run/podman/podman.sock"
                ),
                "docker_socket_visible": _exists_in_container(
                    identity.container_name, "-S", "/var/run/docker.sock"
                ),
                "provider_key_present": bool(
                    env_names
                    & {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}
                ),
            }
            assert initialized.protocol_version == 1
            assert initialized.agent_info is not None
            assert initialized.agent_info.name == "OpenCode"
            assert initialized.agent_info.version in agent.supported_versions
            resume_supported = bool(
                capabilities.get("session_capabilities", {}).get("resume")
            )
            assert resume_supported is agent.resume
            assert workspace.base_commit == source_commit
            assert len(mounts) == 1 and mounts[0].get("Destination") == "/workspace"
            assert container.get("HostConfig", {}).get("NetworkMode") == "none"
            assert not any(isolation.values()), isolation

            print(
                json.dumps(
                    {
                        "agent_info": initialized.agent_info.model_dump(mode="json"),
                        "agent_capabilities": capabilities,
                        "agent_session_resume_supported": resume_supported,
                        "protocol_version": initialized.protocol_version,
                        "session_created": bool(session_id),
                        "prompt_sent": False,
                        "base_commit": workspace.base_commit,
                        "source_commit": source_commit,
                        "image": IMAGE,
                        "image_id": container.get("Image"),
                        "platform": f"{container.get('Platform', 'linux/amd64')}",
                        "network_mode": container["HostConfig"]["NetworkMode"],
                        "mounts": [
                            {"destination": row.get("Destination"), "type": row.get("Type")}
                            for row in mounts
                        ],
                        "limits": {
                            "memory_bytes": container["HostConfig"].get("Memory"),
                            "nano_cpus": container["HostConfig"].get("NanoCpus"),
                            "pids": container["HostConfig"].get("PidsLimit"),
                        },
                        "isolation_checks": isolation,
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
            await backend.stop_and_remove(identity)
            await workspace_manager.remove_workspace(workspace)


if __name__ == "__main__":
    asyncio.run(main())
