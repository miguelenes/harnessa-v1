"""Fail-closed policies applied to per-run workspaces and Podman sandboxes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class SandboxPolicyError(ValueError):
    """A requested operation exceeds the configured workspace boundary."""


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = 2.0
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    pids: int = 256
    tmpfs_bytes: int = 512 * 1024 * 1024
    workspace_bytes: int = 4 * 1024 * 1024 * 1024
    wall_seconds: int = 1800
    max_protocol_frame_bytes: int = 1024 * 1024
    max_agent_updates: int = 10000
    max_artifact_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not math.isfinite(self.cpus) or self.cpus <= 0:
            raise SandboxPolicyError("CPU limit must be a finite positive value")
        if (
            min(
                self.memory_bytes,
                self.pids,
                self.tmpfs_bytes,
                self.workspace_bytes,
                self.wall_seconds,
                self.max_protocol_frame_bytes,
                self.max_agent_updates,
                self.max_artifact_bytes,
            )
            <= 0
        ):
            raise SandboxPolicyError("Sandbox resource limits must be positive")


def resolve_workspace_path(workspace: Path, requested: str) -> Path:
    """Resolve ACP paths rooted at /workspace and reject traversal or symlink escape."""
    if not requested or "\x00" in requested:
        raise SandboxPolicyError("An empty or invalid workspace path was rejected")

    raw = PurePosixPath(requested)
    if raw.is_absolute():
        if raw.parts[:2] != ("/", "workspace"):
            raise SandboxPolicyError("Absolute paths must be rooted at /workspace")
        parts = raw.parts[2:]
    else:
        parts = raw.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SandboxPolicyError("Workspace path traversal was rejected")

    root = workspace.resolve(strict=True)
    candidate = root.joinpath(*parts)
    probe = candidate
    while not probe.exists() and not probe.is_symlink():
        if probe == root:
            break
        probe = probe.parent
    try:
        resolved_probe = probe.resolve(strict=True)
        resolved_probe.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SandboxPolicyError("Workspace symlink escape was rejected") from exc

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SandboxPolicyError("Workspace symlink escape was rejected") from exc
    return resolved


def permission_allowed(kind: str | None, paths: list[str], workspace: Path | None = None) -> bool:
    """Allow only operations contained by the isolated checkout and not network fetches."""
    if kind not in {"read", "edit", "delete", "move", "search", "execute", "switch_mode"}:
        return False
    if kind == "execute" and not paths:
        # Shell execution itself is inside the rootless, network-isolated container.
        return True
    if not paths:
        return False
    try:
        for path in paths:
            if not path.startswith("/workspace"):
                return False
            if path != "/workspace" and not path.startswith("/workspace/"):
                return False
            if any(part in {".", ".."} for part in path.split("/")):
                return False
            if workspace is not None:
                resolve_workspace_path(workspace, path)
    except SandboxPolicyError:
        return False
    return True
