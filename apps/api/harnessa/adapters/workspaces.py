"""Managed, per-run repository clones with no shared Git metadata."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from harnessa.adapters.execution import RunInput, TaskInput

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class WorkspaceError(RuntimeError):
    """The trusted runner could not provision an exact, isolated source tree."""


@dataclass(frozen=True)
class ManagedWorkspace:
    task_id: UUID
    project_id: UUID
    run_id: UUID
    attempt: int
    fencing_token: int
    repository_path: Path
    workspace_path: Path
    state_path: Path
    base_commit: str
    branch: str
    created_at: datetime


class WorkspaceManager:
    def __init__(self, root: Path, *, size_limit_bytes: int = 4 * 1024 * 1024 * 1024) -> None:
        self.root = root.expanduser().resolve()
        self.state_root = self.root / ".runner-state"
        self.size_limit_bytes = size_limit_bytes

    async def provision(
        self,
        task: TaskInput,
        run: RunInput,
        repository_path: Path,
        *,
        requested_base_commit: str | None = None,
    ) -> ManagedWorkspace:
        return await asyncio.to_thread(
            self._provision, task, run, repository_path, requested_base_commit
        )

    def _provision(
        self,
        task: TaskInput,
        run: RunInput,
        repository_path: Path,
        requested_base_commit: str | None,
    ) -> ManagedWorkspace:
        source = repository_path.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise WorkspaceError("The configured repository source is not a directory")
        if self.root == source or self.root.is_relative_to(source):
            raise WorkspaceError("Managed workspaces may not be created inside the source checkout")
        top = _git(source, "rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() != source:
            raise WorkspaceError("Repository sources must map to a Git worktree root")

        candidate = requested_base_commit or _git(source, "rev-parse", "HEAD").strip()
        if not _COMMIT_RE.fullmatch(candidate):
            raise WorkspaceError("The requested base commit must be a full Git object id")
        base_commit = _git(source, "rev-parse", "--verify", f"{candidate}^{{commit}}").strip()
        if base_commit != candidate:
            raise WorkspaceError("The requested base commit did not resolve exactly")

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.state_root, 0o700)
        workspace = self.root / f"{run.id.hex}-f{run.fencing_token}"
        state_path = self.state_root / f"{run.id.hex}-f{run.fencing_token}.json"
        if workspace.exists() or state_path.exists():
            raise WorkspaceError("A workspace already exists for this run fence")

        try:
            workspace.mkdir(mode=0o700)
            object_format = _git(source, "rev-parse", "--show-object-format").strip()
            init_args = ["init", "--quiet"]
            if object_format != "sha1":
                init_args.append(f"--object-format={object_format}")
            _git(workspace, *init_args)
            os.chmod(workspace, 0o700)
            _git(
                workspace,
                "-c",
                "core.hooksPath=/dev/null",
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                str(source),
                base_commit,
            )
            _git(workspace, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", base_commit)
            _git(workspace, "-c", "core.hooksPath=/dev/null", "checkout", "-b", _branch(run))
            for key, value in (
                ("user.name", "Harnessa Agent"),
                ("user.email", "agent@harnessa.invalid"),
                ("credential.helper", ""),
                ("core.hooksPath", "/dev/null"),
            ):
                _git(workspace, "config", key, value)
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

        created_at = datetime.now(UTC)
        record = ManagedWorkspace(
            task_id=task.id,
            project_id=task.project_id,
            run_id=run.id,
            attempt=run.attempt,
            fencing_token=run.fencing_token,
            repository_path=source,
            workspace_path=workspace,
            state_path=state_path,
            base_commit=base_commit,
            branch=_branch(run),
            created_at=created_at,
        )
        _write_manifest(record, status="provisioned")
        return record

    async def remove_workspace(self, workspace: ManagedWorkspace) -> None:
        await asyncio.to_thread(self._remove_workspace, workspace)

    def _remove_workspace(self, workspace: ManagedWorkspace) -> None:
        resolved = workspace.workspace_path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError("Refusing to remove a path outside the managed root") from exc
        if resolved.is_symlink():
            raise WorkspaceError("Refusing to remove a symlinked workspace")
        shutil.rmtree(resolved, ignore_errors=False)
        with contextlib.suppress(FileNotFoundError):
            workspace.state_path.unlink()

    async def mark_status(self, workspace: ManagedWorkspace, status: str) -> None:
        await asyncio.to_thread(_write_manifest, workspace, status)

    async def changed_files(self, workspace: ManagedWorkspace) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_changed_files, workspace)

    async def patch(self, workspace: ManagedWorkspace, limit: int) -> bytes:
        return await asyncio.to_thread(_patch, workspace, limit)

    async def capture_result_revision(self, workspace: ManagedWorkspace) -> str:
        """Store a fenced result commit object in the connected repository without moving refs."""
        return await asyncio.to_thread(_capture_result_revision, workspace)

    async def stale_manifests(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._stale_manifests)

    async def size_bytes(self, workspace: ManagedWorkspace) -> int:
        return await asyncio.to_thread(_size_bytes, workspace, self.size_limit_bytes)

    def _stale_manifests(self) -> list[dict[str, Any]]:
        if not self.state_root.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in self.state_root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    records.append(payload)
            except (OSError, json.JSONDecodeError):
                continue
        return records


def _branch(run: RunInput) -> str:
    return f"harnessa/run-{run.id.hex}-attempt-{run.attempt}-fence-{run.fencing_token}"


def _git(path: Path, *args: str, allow_failure: bool = False) -> str:
    import subprocess

    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TEMPLATE_DIR": "/dev/null",
    }
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        env=env,
        timeout=120,
    )
    if result.returncode and not allow_failure:
        detail = result.stderr.strip()[:500]
        raise WorkspaceError(f"Git workspace operation failed ({args[0]}): {detail}")
    return result.stdout


def _write_manifest(workspace: ManagedWorkspace, status: str) -> None:
    payload = {
        "schema_version": 1,
        "task_id": str(workspace.task_id),
        "project_id": str(workspace.project_id),
        "run_id": str(workspace.run_id),
        "attempt": workspace.attempt,
        "fencing_token": workspace.fencing_token,
        "repository_path": str(workspace.repository_path),
        "workspace_path": str(workspace.workspace_path),
        "base_commit": workspace.base_commit,
        "branch": workspace.branch,
        "created_at": workspace.created_at.isoformat(),
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    temporary = workspace.state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, workspace.state_path)


def _changed_files(workspace: ManagedWorkspace) -> list[dict[str, Any]]:
    raw = _bounded_git_output(
        workspace.workspace_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        limit_bytes=16 * 1024 * 1024,
    )
    entries = raw.split(b"\x00")
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            continue
        status = entry[:2].decode("ascii", errors="replace").strip() or "??"
        relative = entry[3:].decode("utf-8", errors="replace")
        if status and any(char in status for char in "RC") and index < len(entries):
            index += 1
        safe = Path(relative)
        if safe.is_absolute() or ".." in safe.parts or ".git" in safe.parts:
            raise WorkspaceError("Git returned a path outside the managed workspace")
        file_path = workspace.workspace_path / safe
        if file_path.is_symlink():
            content_hash = hashlib.sha256(os.readlink(file_path).encode()).hexdigest()
            size = len(os.readlink(file_path).encode())
        elif file_path.is_file():
            hasher = hashlib.sha256()
            size = 0
            with file_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
                    size += len(chunk)
            content_hash = hasher.hexdigest()
        else:
            content_hash = hashlib.sha256(b"").hexdigest()
            size = 0
        files.append(
            {
                "path": safe.as_posix(),
                "status": status,
                "size_bytes": size,
                "sha256": content_hash,
            }
        )
        if len(files) > 10000:
            raise WorkspaceError("The workspace changed-file manifest exceeded 10,000 entries")
    return files


def _patch(workspace: ManagedWorkspace, limit: int) -> bytes:
    import subprocess

    paths = [item["path"] for item in _changed_files(workspace) if item["status"] == "??"]
    if paths:
        add = subprocess.run(
            [
                "git",
                "-C",
                str(workspace.workspace_path),
                "-c",
                "core.hooksPath=/dev/null",
                "add",
                "--intent-to-add",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=b"\x00".join(os.fsencode(path) for path in paths),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": os.defpath,
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
            timeout=120,
            check=False,
        )
        if add.returncode:
            raise WorkspaceError("Could not prepare bounded untracked-file patch output")
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    process = subprocess.Popen(
        [
            "git",
            "-C",
            str(workspace.workspace_path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "diff.external=",
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert process.stdout is not None
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := process.stdout.read(min(64 * 1024, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                process.kill()
                process.wait(timeout=5)
                raise WorkspaceError("The workspace patch exceeded the configured artifact limit")
        result = process.wait(timeout=120)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    if result:
        raise WorkspaceError("Could not construct the bounded workspace patch")
    return b"".join(chunks)


def _capture_result_revision(workspace: ManagedWorkspace) -> str:
    """Apply the bounded workspace patch to a fresh trusted clone and import its commit object."""
    import subprocess
    import tempfile

    patch = _patch(workspace, 10 * 1024 * 1024)
    if not patch:
        return workspace.base_commit
    with tempfile.TemporaryDirectory(prefix="harnessa-result-") as temporary:
        result_repo = Path(temporary) / "result"
        env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TEMPLATE_DIR": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Harnessa Agent",
            "GIT_AUTHOR_EMAIL": "agent@harnessa.invalid",
            "GIT_COMMITTER_NAME": "Harnessa Runner",
            "GIT_COMMITTER_EMAIL": "runner@harnessa.invalid",
        }

        def git(path: Path, *args: str, input_bytes: bytes | None = None) -> str:
            process = subprocess.run(
                ["git", "-C", str(path), *args],
                check=False,
                input=input_bytes,
                capture_output=True,
                env=env,
                timeout=120,
            )
            if process.returncode:
                raise WorkspaceError("Could not capture the trusted implementation revision")
            return process.stdout.decode("ascii", errors="strict").strip()

        git(
            result_repo.parent,
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            str(workspace.repository_path),
            str(result_repo),
        )
        git(
            result_repo,
            "-c",
            "core.hooksPath=/dev/null",
            "checkout",
            "--quiet",
            "--detach",
            workspace.base_commit,
        )
        apply = subprocess.run(
            [
                "git",
                "-C",
                str(result_repo),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.attributesFile=/dev/null",
                "apply",
                "--index",
                "--binary",
                "-",
            ],
            input=patch,
            check=False,
            capture_output=True,
            env=env,
            timeout=120,
        )
        if apply.returncode:
            raise WorkspaceError("The workspace patch did not apply cleanly to its exact base")
        tree = git(result_repo, "write-tree")
        revision = git(
            result_repo,
            "commit-tree",
            tree,
            "-p",
            workspace.base_commit,
            "-m",
            f"Harnessa run {workspace.run_id} attempt {workspace.attempt}",
        )
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise WorkspaceError("Captured result revision was not a full Git object ID")
        git(
            workspace.repository_path,
            "-c",
            "core.hooksPath=/dev/null",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            str(result_repo),
            revision,
        )
        verified = git(workspace.repository_path, "cat-file", "-t", revision)
        if verified != "commit":
            raise WorkspaceError("Captured result revision is missing from its repository")
        return revision


def _size_bytes(workspace: ManagedWorkspace, stop_after: int) -> int:
    """Measure logical file sizes without following symlinks or opening file content."""
    root = workspace.workspace_path.resolve(strict=True)
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise WorkspaceError("Could not measure the managed workspace") from exc
        for entry in entries:
            try:
                item = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceError("Could not measure the managed workspace") from exc
            if stat.S_ISDIR(item.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(item.st_mode):
                total += item.st_size
                if total > stop_after:
                    return total
    return total


def _bounded_git_output(path: Path, *args: str, limit_bytes: int) -> bytes:
    import subprocess

    process = subprocess.Popen(
        ["git", "-C", str(path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    assert process.stdout is not None
    chunks: list[bytes] = []
    size = 0
    try:
        while chunk := process.stdout.read(min(64 * 1024, limit_bytes + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit_bytes:
                process.kill()
                process.wait(timeout=5)
                raise WorkspaceError("The changed-file manifest exceeded its output bound")
        if process.wait(timeout=120):
            raise WorkspaceError("Could not read the managed workspace change manifest")
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    return b"".join(chunks)
