from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from harnessa.adapters.execution import RunInput, TaskInput
from harnessa.adapters.podman_sandbox import PodmanSandboxBackend, SandboxIdentity
from harnessa.adapters.sandbox_policy import SandboxPolicyError, SandboxLimits
from harnessa.adapters.workspaces import WorkspaceError, WorkspaceManager


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    return result.stdout.strip()


def _source_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Harnessa Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("committed base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "--quiet", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


@pytest.mark.anyio
async def test_workspace_is_independent_shallow_and_exact_at_base_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base = _source_repo(source)
    (source / "README.md").write_text("uncommitted user content\n", encoding="utf-8")
    outside = tmp_path / "host-secret.txt"
    outside.write_text("never mounted", encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "managed")
    task = TaskInput(uuid4(), uuid4(), "fixture", "description")
    run = RunInput(uuid4(), attempt=1, fencing_token=7)

    workspace = await manager.provision(task, run, source)
    try:
        assert workspace.base_commit == base
        assert _git(workspace.workspace_path, "rev-parse", "HEAD") == base
        assert _git(workspace.workspace_path, "branch", "--show-current") == workspace.branch
        assert _git(workspace.workspace_path, "remote") == ""
        assert _git(workspace.workspace_path, "rev-list", "--count", "HEAD") == "1"
        assert (workspace.workspace_path / ".git" / "shallow").exists()
        assert (workspace.workspace_path / "README.md").read_text(
            encoding="utf-8"
        ) == "committed base\n"
        assert (source / "README.md").read_text(encoding="utf-8") == "uncommitted user content\n"
        assert (source / ".git").resolve() != (workspace.workspace_path / ".git").resolve()
        (workspace.workspace_path / "new file.txt").write_text("agent change\n", encoding="utf-8")
        changed = await manager.changed_files(workspace)
        assert changed[0]["path"] == "new file.txt"
        assert (await manager.patch(workspace, 1024)).startswith(b"diff --git")
        assert await manager.size_bytes(workspace) > 0
        assert outside.read_text(encoding="utf-8") == "never mounted"
    finally:
        await manager.remove_workspace(workspace)


@pytest.mark.anyio
async def test_workspace_root_inside_source_and_unbounded_policy_are_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _source_repo(source)
    manager = WorkspaceManager(source / "managed")
    with pytest.raises(WorkspaceError, match="inside the source checkout"):
        await manager.provision(
            TaskInput(uuid4(), uuid4(), "fixture", "description"),
            RunInput(uuid4(), attempt=1, fencing_token=1),
            source,
        )
    with pytest.raises(SandboxPolicyError):
        SandboxLimits(memory_bytes=0)


def test_podman_command_exposes_only_workspace_with_no_engine_or_network_access(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    digest = "a" * 64
    backend = PodmanSandboxBackend(
        image=f"registry.invalid/agent@sha256:{digest}", owner_id="installation"
    )
    identity = SandboxIdentity(
        owner_id="installation",
        run_id=str(uuid4()),
        task_id=str(uuid4()),
        attempt=1,
        fencing_token=2,
    )
    command = backend.command(
        identity=identity,
        workspace=workspace,
        state_root=state,
        agent_command=("acp",),
    )
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    volume_index = command.index("--volume")
    assert command[volume_index + 1] == f"{workspace.resolve()}:/workspace:rw,rprivate,Z"
    assert command.count("--volume") == 1
    assert not any("podman.sock" in item or "docker.sock" in item for item in command)
    assert not any(
        item.startswith("OPENAI_API_KEY=") or item.startswith("ANTHROPIC_API_KEY=")
        for item in command
    )
    with pytest.raises(SandboxPolicyError, match="digest"):
        PodmanSandboxBackend(image="registry.invalid/agent:latest", owner_id="installation")
