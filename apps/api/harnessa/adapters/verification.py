"""Administrator-approved, revision-bound command verification in rootless Podman."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harnessa.services.retrieval_governance import safe_retrieval_content

_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_COMMANDS = 50
_MAX_CRITERIA = 200
_MAX_ARG_BYTES = 16_384


def _podman_environment(*, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in (
        "XDG_RUNTIME_DIR",
        "CONTAINERS_STORAGE_CONF",
        "CONTAINERS_CONF",
        "CONTAINERS_CONF_OVERRIDE",
        "CONTAINERS_REGISTRIES_CONF",
        "TMPDIR",
    ):
        if value := os.environ.get(key):
            env[key] = value
    env.update(extra or {})
    return env


async def _communicate_process(
    process: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    try:
        result = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return result


async def _wait_process(process: asyncio.subprocess.Process, timeout: float) -> int:
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


class VerificationPolicyError(ValueError):
    """A verifier manifest or execution request violates the trusted policy."""


class VerificationUnavailable(RuntimeError):
    """The local rootless runtime or an approved, digest-pinned image is unavailable."""


class VerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=300)
    criteria: tuple[str, ...] = Field(min_length=1, max_length=200)
    artifact_paths: tuple[str, ...] = Field(default=(), max_length=50)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    output_limit_bytes: int = Field(default=64_000, ge=1024, le=1_000_000)
    required: bool = True

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if not _IMAGE_DIGEST.fullmatch(value):
            raise ValueError("Verification images must be pinned by sha256 digest")
        return value

    @field_validator("argv")
    @classmethod
    def safe_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not arg or "\x00" in arg or len(arg.encode()) > _MAX_ARG_BYTES for arg in value):
            raise ValueError("Verification arguments must be nonempty bounded strings")
        if value[0].startswith("-") or "/" in value[0] and not value[0].startswith("/"):
            raise ValueError("The verifier entrypoint must be an absolute path or command name")
        return value

    @field_validator("artifact_paths")
    @classmethod
    def safe_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            path = PurePosixPath(item)
            if not item or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise ValueError("Verification artifact paths must be safe relative paths")
        if len(set(value)) != len(value):
            raise ValueError("Verification artifact paths must be unique")
        return value


class VerificationCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    requirement: str = Field(min_length=1, max_length=1000)
    scenario: str = Field(min_length=1, max_length=1000)
    manual_review: bool = False


class VerificationManifest(BaseModel):
    """Immutable snapshot of an administrator-approved repository test policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    criteria: tuple[VerificationCriterion, ...] = Field(min_length=1, max_length=_MAX_CRITERIA)
    commands: tuple[VerificationCommand, ...] = Field(min_length=1, max_length=_MAX_COMMANDS)

    @model_validator(mode="after")
    def references_resolve(self) -> VerificationManifest:
        criterion_ids = [criterion.id for criterion in self.criteria]
        command_ids = [command.id for command in self.commands]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("Verification criterion IDs must be unique")
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("Verification command IDs must be unique")
        known = set(criterion_ids)
        if any(set(command.criteria) - known for command in self.commands):
            raise ValueError("Verification commands must reference declared criteria")
        if any(
            not any(
                criterion_id in command.criteria and command.required for command in self.commands
            )
            and not criterion.manual_review
            for criterion in self.criteria
            for criterion_id in (criterion.id,)
        ):
            raise ValueError("Every non-manual criterion must be covered by an approved command")
        return self

    @property
    def digest(self) -> str:
        canonical = self.model_dump_json(exclude={"revision"}, by_alias=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


VerificationOutcome = Literal["passed", "failed", "manual_review_required", "unverified"]


@dataclass(frozen=True)
class CommandEvidence:
    command_id: str
    image: str
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_ms: int
    outcome: Literal["passed", "failed", "unverified"]
    artifacts: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class CriterionEvidence:
    criterion_id: str
    outcome: VerificationOutcome
    summary: str
    command_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerificationResult:
    source_revision: str
    manifest_revision: int
    manifest_digest: str
    outcome: VerificationOutcome
    commands: tuple[CommandEvidence, ...]
    criteria: tuple[CriterionEvidence, ...]
    content_hash: str


class CommandExecutor(Protocol):
    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence: ...


class PodmanCommandExecutor:
    """Run a manifest command without a shell in an isolated, networkless container."""

    def __init__(
        self,
        *,
        executable: str = "podman",
        cpus: float = 2,
        memory_bytes: int = 2 * 1024**3,
        pids_limit: int = 256,
        tmpfs_bytes: int = 512 * 1024**2,
        workspace_bytes: int = 1024**3,
    ) -> None:
        self.executable = shutil.which(executable) or executable
        self.cpus = cpus
        self.memory_bytes = memory_bytes
        self.pids_limit = pids_limit
        self.tmpfs_bytes = tmpfs_bytes
        if workspace_bytes < 16 * 1024**2 or workspace_bytes > 4 * 1024**3:
            raise VerificationPolicyError("Verifier workspace size must be between 16 MB and 4 GB")
        self.workspace_bytes = workspace_bytes

    async def validate(self, manifest: VerificationManifest) -> None:
        if not shutil.which(self.executable) and not Path(self.executable).is_file():
            raise VerificationUnavailable("Podman is unavailable; verification did not run")
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "info",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError) as exc:
            raise VerificationUnavailable(
                "Rootless Podman readiness could not be verified"
            ) from exc
        try:
            rootless = process.returncode == 0 and bool(
                json.loads(stdout)["host"]["security"]["rootless"]
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            rootless = False
        if not rootless:
            raise VerificationUnavailable("Independent verification requires rootless Podman")
        for image in sorted({command.image for command in manifest.commands}):
            image_check = await asyncio.create_subprocess_exec(
                self.executable,
                "image",
                "exists",
                image,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await image_check.wait() != 0:
                raise VerificationUnavailable(
                    "An administrator-approved verification image is not present locally"
                )

    def command(self, command: VerificationCommand, workspace: Path) -> list[str]:
        path = workspace.resolve(strict=True)
        if any(char in str(path) for char in ",:"):
            raise VerificationPolicyError("Verification workspace path has unsupported characters")
        argv = [
            self.executable,
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--userns=keep-id",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            str(self.memory_bytes),
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.tmpfs_bytes},mode=1777",
            "--env",
            "HOME=/tmp",
            "--volume",
            f"{path}:/workspace:rw,rprivate,Z",
        ]
        fixture_path = path.parent / "protected-fixtures"
        if fixture_path.is_dir():
            if any(char in str(fixture_path) for char in ",:"):
                raise VerificationPolicyError("Protected fixture path has unsupported characters")
            argv.extend(["--volume", f"{fixture_path}:/verification-fixtures:ro,rprivate,Z"])
        argv.extend(
            [
                "--workdir",
                "/workspace",
                "--entrypoint",
                command.argv[0],
                command.image,
                *command.argv[1:],
            ]
        )
        return argv

    def volume_command(
        self, command: VerificationCommand, volume: str, fixture_path: Path | None = None
    ) -> list[str]:
        if not re.fullmatch(r"harnessa-verify-[a-f0-9]{32}", volume):
            raise VerificationPolicyError("Verification workspace volume name is invalid")
        argv = [
            self.executable,
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--userns=keep-id",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            str(self.memory_bytes),
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.tmpfs_bytes},mode=1777",
            "--env",
            "HOME=/tmp",
            "--volume",
            f"{volume}:/workspace:rw,rprivate,Z",
        ]
        if fixture_path is not None:
            resolved = fixture_path.resolve(strict=True)
            if any(char in str(resolved) for char in ",:"):
                raise VerificationPolicyError("Protected fixture path has unsupported characters")
            argv.extend(["--volume", f"{resolved}:/verification-fixtures:ro,rprivate,Z"])
        argv.extend(
            [
                "--workdir",
                "/workspace",
                "--entrypoint",
                command.argv[0],
                command.image,
                *command.argv[1:],
            ]
        )
        return argv

    async def execute_revision(
        self,
        command: VerificationCommand,
        *,
        repository_path: Path,
        source_revision: str,
        protected_fixtures: Mapping[str, bytes],
        max_total_seconds: int = 1800,
        max_artifact_bytes: int = 1_000_000,
    ) -> tuple[CommandEvidence, tuple[dict[str, object], ...], int, bool]:
        """Clone, execute, and collect evidence inside a quota-limited tmpfs volume."""
        volume = "harnessa-verify-" + os.urandom(16).hex()
        source = repository_path.expanduser().resolve(strict=True)
        if not _SHA.fullmatch(source_revision):
            raise VerificationPolicyError("Verification requires an exact full source commit SHA")
        provision_script = r"""import os, subprocess, sys
podman, volume, source, revision, timeout = sys.argv[1:]
timeout = int(timeout)
env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_TEMPLATE_DIR": "/dev/null", "GIT_ALLOW_PROTOCOL": "file"}
def run(args, timeout=300):
    return subprocess.run(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
try:
    stage = "mount"
    mount = subprocess.run([podman, "volume", "mount", volume], capture_output=True, check=True, timeout=30).stdout.decode().strip()
    target = mount
    stage = "init"
    run(["git", "init", "--quiet", target], timeout=timeout)
    stage = "fetch"
    run(["git", "-C", target, "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules", source, revision], timeout=timeout)
    stage = "checkout"
    run(["git", "-C", target, "-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", "FETCH_HEAD"], timeout=timeout)
except Exception as exc:
    print(stage + ":" + type(exc).__name__, file=sys.stderr)
    sys.exit(23)
"""
        artifact_script = r"""import base64, hashlib, json, os, subprocess, sys
podman, volume, *paths = sys.argv[1:]
stage = "mount"
try:
    mount = subprocess.run([podman, "volume", "mount", volume], capture_output=True, check=True, timeout=30).stdout.decode().strip()
    root = os.path.realpath(mount)
    records = []
    total = 0
    missing = False
    limit = int(os.environ["HARNessa_ARTIFACT_LIMIT"])
    for relative in paths:
        stage = "artifact"
        target = os.path.join(root, *relative.split("/"))
        if os.path.islink(target):
            raise ValueError()
        if not os.path.exists(target):
            missing = True
            records.append({"path": relative, "status": "missing"})
            continue
        resolved = os.path.realpath(target)
        if os.path.commonpath([root, resolved]) != root or not os.path.isfile(resolved):
            raise ValueError()
        size = os.path.getsize(resolved)
        total += size
        if total > limit:
            raise ValueError()
        with open(resolved, "rb") as stream:
            content = stream.read(limit + 1)
        if len(content) != size:
            raise ValueError()
        records.append({"path": relative, "status": "captured", "size_bytes": size, "sha256": hashlib.sha256(content).hexdigest(), "content_base64": base64.b64encode(content).decode("ascii")})
    print(json.dumps({"records": records, "bytes": total, "missing": missing}, separators=(",", ":")))
except Exception as exc:
    print(stage + ":" + type(exc).__name__, file=sys.stderr)
    sys.exit(24)
"""
        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stdout = b""
        stderr = b""
        records: tuple[dict[str, object], ...] = ()
        artifact_bytes = 0
        artifact_missing = False
        fixture_temporary = TemporaryDirectory(prefix="harnessa-fixtures-")
        fixture_root = Path(fixture_temporary.name) / "protected-fixtures"
        if protected_fixtures:
            fixture_root.mkdir(mode=0o700)
            for relative_path, content in protected_fixtures.items():
                target = fixture_root / PurePosixPath(relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.resolve().is_relative_to(fixture_root.resolve()):
                    fixture_temporary.cleanup()
                    raise VerificationPolicyError("Protected fixture escaped its mount root")
                target.write_bytes(content)
                target.chmod(0o400)
            for directory in sorted(
                (path for path in fixture_root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.chmod(0o500)
            fixture_root.chmod(0o500)
        fixture_mount = fixture_root if protected_fixtures else None
        try:
            volume_create = await asyncio.create_subprocess_exec(
                self.executable,
                "volume",
                "create",
                "--label",
                "harnessa.purpose=verification-workspace",
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                f"o=size={self.workspace_bytes},nodev,nosuid",
                volume,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_podman_environment(),
            )
            _create_out, _create_err = await _communicate_process(volume_create, timeout=30)
            if volume_create.returncode != 0:
                raise VerificationUnavailable("Could not allocate a bounded verifier workspace")

            provision = await asyncio.create_subprocess_exec(
                self.executable,
                "unshare",
                sys.executable,
                "-c",
                provision_script,
                self.executable,
                volume,
                str(source),
                source_revision,
                str(max(1, min(300, max_total_seconds))),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_podman_environment(),
            )
            _provision_out, provision_err = await _communicate_process(
                provision, timeout=min(300, max_total_seconds) + 30
            )
            if provision.returncode != 0:
                detail = provision_err.decode("utf-8", errors="replace")[:100]
                raise VerificationUnavailable(
                    "Could not materialize the revision within the verifier workspace limit"
                    + (f" ({detail})" if detail else "")
                )

            command_remaining = max_total_seconds - int(time.monotonic() - started)
            if command_remaining < 1:
                return (
                    CommandEvidence(
                        command_id=command.id,
                        image=command.image,
                        exit_code=None,
                        timed_out=False,
                        stdout="",
                        stderr="Shared verification time limit exhausted during bounded checkout",
                        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                        outcome="unverified",
                    ),
                    (),
                    0,
                    bool(command.artifact_paths),
                )

            process = await asyncio.create_subprocess_exec(
                *self.volume_command(command, volume, fixture_mount),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_podman_environment(),
            )
            stdout_task = asyncio.create_task(
                _read_bounded(process.stdout, command.output_limit_bytes)
            )
            stderr_task = asyncio.create_task(
                _read_bounded(process.stderr, command.output_limit_bytes)
            )
            try:
                await _wait_process(
                    process, timeout=min(command.timeout_seconds, command_remaining)
                )
            except TimeoutError:
                timed_out = True
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            exit_code = process.returncode

            if command.artifact_paths:
                capture = await asyncio.create_subprocess_exec(
                    self.executable,
                    "unshare",
                    sys.executable,
                    "-c",
                    artifact_script,
                    self.executable,
                    volume,
                    *command.artifact_paths,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_podman_environment(
                        extra={"HARNessa_ARTIFACT_LIMIT": str(max_artifact_bytes)}
                    ),
                )
                capture_out, capture_err = await _communicate_process(capture, timeout=45)
                if capture.returncode != 0:
                    detail = capture_err.decode("utf-8", errors="replace")[:100]
                    raise VerificationPolicyError(
                        "Verification artifacts exceeded their bounded safe capture"
                        + (f" ({detail})" if detail else "")
                    )
                artifact_result = json.loads(capture_out)
                artifact_bytes = int(artifact_result["bytes"])
                artifact_missing = bool(artifact_result["missing"])
                captured: list[dict[str, object]] = []
                for item in artifact_result["records"]:
                    record = dict(item)
                    encoded = record.pop("content_base64", None)
                    if encoded is not None:
                        content = base64.b64decode(str(encoded), validate=True)
                        try:
                            safe_to_store = safe_retrieval_content(content.decode("utf-8"))
                        except UnicodeDecodeError:
                            safe_to_store = False
                        record["content_base64"] = str(encoded) if safe_to_store else ""
                        record["redacted"] = not safe_to_store
                    captured.append(record)
                records = tuple(captured)
        except TimeoutError as exc:
            raise VerificationUnavailable(
                "Bounded verifier workspace preparation or evidence capture timed out"
            ) from exc
        finally:
            remove = await asyncio.create_subprocess_exec(
                self.executable,
                "volume",
                "rm",
                volume,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_podman_environment(),
            )
            await _wait_process(remove, timeout=30)
            fixture_temporary.cleanup()

        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        outcome: Literal["passed", "failed", "unverified"] = (
            "unverified"
            if exit_code is None
            else "failed"
            if timed_out or exit_code != 0
            else "passed"
        )
        evidence = CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=_redact(stdout),
            stderr=_redact(stderr),
            duration_ms=duration_ms,
            outcome=outcome,
            artifacts=records,
        )
        return evidence, records, artifact_bytes, artifact_missing

    async def execute(self, command: VerificationCommand, workspace: Path) -> CommandEvidence:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(command, workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PATH": os.defpath,
                    "HOME": str(Path.home()),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
            stdout_task = asyncio.create_task(
                _read_bounded(process.stdout, command.output_limit_bytes)
            )
            stderr_task = asyncio.create_task(
                _read_bounded(process.stderr, command.output_limit_bytes)
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=command.timeout_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()
                timed_out = True
            else:
                timed_out = False
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            exit_code = process.returncode
        except FileNotFoundError:
            timed_out = False
            exit_code = None
            stdout = b""
            stderr = b"Podman is unavailable; verification did not run"
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        outcome: Literal["passed", "failed", "unverified"] = (
            "unverified"
            if exit_code is None
            else "failed"
            if timed_out or exit_code != 0
            else "passed"
        )
        return CommandEvidence(
            command_id=command.id,
            image=command.image,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=_redact(stdout),
            stderr=_redact(stderr),
            duration_ms=duration_ms,
            outcome=outcome,
        )


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    kept = bytearray()
    while chunk := await stream.read(64 * 1024):
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
    return bytes(kept)


def _redact(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    if not safe_retrieval_content(text):
        return "[output redacted by the credential safety filter]"
    return text


def _capture_artifacts(
    workspace: Path, paths: tuple[str, ...], *, max_bytes: int
) -> tuple[tuple[dict[str, object], ...], int, bool]:
    records: list[dict[str, object]] = []
    total = 0
    missing = False
    root = workspace.resolve(strict=True)
    for relative in paths:
        target = workspace / PurePosixPath(relative)
        if target.is_symlink():
            raise VerificationPolicyError("A verification artifact may not be a symlink")
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            missing = True
            records.append({"path": relative, "status": "missing"})
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise VerificationPolicyError("A declared verification artifact is missing or unsafe")
        content = resolved.read_bytes()
        if total + len(content) > max_bytes:
            raise VerificationPolicyError("Verification artifacts exceed the shared evidence limit")
        total += len(content)
        try:
            safe_text = content.decode("utf-8")
            safe_to_store = safe_retrieval_content(safe_text)
        except UnicodeDecodeError:
            safe_to_store = False
        records.append(
            {
                "path": relative,
                "status": "captured",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii")
                if safe_to_store
                else "",
                "redacted": not safe_to_store,
            }
        )
    return tuple(records), total, missing


class VerificationRunner:
    def __init__(
        self,
        executor: CommandExecutor,
        *,
        max_total_seconds: int = 1800,
        max_output_bytes: int = 4_000_000,
    ) -> None:
        if max_total_seconds < 1 or max_output_bytes < 1024:
            raise VerificationPolicyError("Verification limits must be positive")
        self.executor = executor
        self.max_total_seconds = max_total_seconds
        self.max_output_bytes = max_output_bytes

    async def run(
        self,
        manifest: VerificationManifest,
        *,
        repository_path: Path,
        source_revision: str,
        protected_fixtures: Mapping[str, bytes] | None = None,
    ) -> VerificationResult:
        if not _SHA.fullmatch(source_revision):
            raise VerificationPolicyError("Verification requires an exact full source commit SHA")
        try:
            source = repository_path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise VerificationPolicyError(
                "The configured verifier repository is unavailable"
            ) from exc
        if not source.is_dir():
            raise VerificationPolicyError("The configured verifier repository is unavailable")
        fixtures = dict(protected_fixtures or {})
        if len(fixtures) > 100 or sum(len(content) for content in fixtures.values()) > 2_000_000:
            raise VerificationPolicyError("Protected fixture set exceeds its approved limit")
        for relative_path, content in fixtures.items():
            parsed = PurePosixPath(relative_path)
            if (
                not relative_path
                or parsed.is_absolute()
                or ".." in parsed.parts
                or ".git" in parsed.parts
                or not isinstance(content, bytes)
            ):
                raise VerificationPolicyError("Protected fixture path or content is invalid")
        with TemporaryDirectory(prefix="harnessa-verify-") as temporary:
            root = Path(temporary)
            if fixtures:
                fixture_root = root / "protected-fixtures"
                fixture_root.mkdir(mode=0o700)
                for relative_path, content in fixtures.items():
                    target = fixture_root / relative_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.resolve().is_relative_to(fixture_root.resolve()):
                        raise VerificationPolicyError("Protected fixture escaped its mount root")
                    target.write_bytes(content)
                    target.chmod(0o400)
                for directory in sorted(
                    (path for path in fixture_root.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    directory.chmod(0o500)
                fixture_root.chmod(0o500)
            commands: list[CommandEvidence] = []
            started = time.monotonic()
            total_output = 0
            availability_error: str | None = None
            validate = getattr(self.executor, "validate", None)
            if validate is not None:
                try:
                    await validate(manifest)
                except VerificationUnavailable as exc:
                    availability_error = str(exc)
            for command in manifest.commands:
                if availability_error is not None:
                    commands.append(
                        CommandEvidence(
                            command_id=command.id,
                            image=command.image,
                            exit_code=None,
                            timed_out=False,
                            stdout="",
                            stderr=_redact(availability_error.encode()),
                            duration_ms=0,
                            outcome="unverified",
                        )
                    )
                    continue
                elapsed = time.monotonic() - started
                if elapsed >= self.max_total_seconds:
                    commands.append(
                        CommandEvidence(
                            command_id=command.id,
                            image=command.image,
                            exit_code=None,
                            timed_out=False,
                            stdout="",
                            stderr="Shared verification time limit exhausted",
                            duration_ms=0,
                            outcome="unverified",
                        )
                    )
                    continue
                bounded_command = command.model_copy(
                    update={
                        "timeout_seconds": min(
                            command.timeout_seconds, max(1, self.max_total_seconds - int(elapsed))
                        )
                    }
                )
                elapsed = time.monotonic() - started
                if elapsed >= self.max_total_seconds:
                    commands.append(
                        CommandEvidence(
                            command_id=command.id,
                            image=command.image,
                            exit_code=None,
                            timed_out=False,
                            stdout="",
                            stderr="Shared verification time limit exhausted before command execution",
                            duration_ms=0,
                            outcome="unverified",
                        )
                    )
                    continue
                bounded_command = command.model_copy(
                    update={
                        "timeout_seconds": min(
                            command.timeout_seconds, max(1, self.max_total_seconds - int(elapsed))
                        )
                    }
                )
                workspace = root / command.id
                execute_revision = getattr(self.executor, "execute_revision", None)
                if callable(execute_revision):
                    evidence, artifacts, artifact_bytes, artifact_missing = await execute_revision(
                        bounded_command,
                        repository_path=source,
                        source_revision=source_revision,
                        protected_fixtures=fixtures,
                        max_total_seconds=max(
                            1, self.max_total_seconds - int(time.monotonic() - started)
                        ),
                        max_artifact_bytes=1_000_000,
                    )
                else:
                    remaining = max(1, self.max_total_seconds - int(time.monotonic() - started))
                    await _git(
                        "clone",
                        "--quiet",
                        "--no-hardlinks",
                        "--no-checkout",
                        str(source),
                        str(workspace),
                        timeout_seconds=remaining,
                    )
                    remaining = max(1, self.max_total_seconds - int(time.monotonic() - started))
                    await _git(
                        "-C",
                        str(workspace),
                        "checkout",
                        "--quiet",
                        "--detach",
                        source_revision,
                        timeout_seconds=remaining,
                    )
                    evidence = await self.executor.execute(bounded_command, workspace)
                    artifacts, artifact_bytes, artifact_missing = _capture_artifacts(
                        workspace, command.artifact_paths, max_bytes=1_000_000
                    )
                evidence = CommandEvidence(
                    command_id=evidence.command_id,
                    image=evidence.image,
                    exit_code=evidence.exit_code,
                    timed_out=evidence.timed_out,
                    stdout=evidence.stdout,
                    stderr=evidence.stderr,
                    duration_ms=evidence.duration_ms,
                    outcome=(
                        "unverified"
                        if artifact_missing and evidence.outcome == "passed"
                        else evidence.outcome
                    ),
                    artifacts=artifacts,
                )
                total_output += artifact_bytes
                total_output += len(evidence.stdout.encode()) + len(evidence.stderr.encode())
                if total_output > self.max_output_bytes:
                    evidence = CommandEvidence(
                        command_id=evidence.command_id,
                        image=evidence.image,
                        exit_code=evidence.exit_code,
                        timed_out=evidence.timed_out,
                        stdout="[output omitted: shared evidence limit exceeded]",
                        stderr="",
                        duration_ms=evidence.duration_ms,
                        outcome=evidence.outcome,
                        artifacts=evidence.artifacts,
                    )
                commands.append(evidence)
        by_command = {entry.command_id: entry for entry in commands}
        criteria: list[CriterionEvidence] = []
        for criterion in manifest.criteria:
            relevant = [
                command for command in manifest.commands if criterion.id in command.criteria
            ]
            results = [by_command[command.id] for command in relevant if command.id in by_command]
            command_ids = tuple(command.id for command in relevant)
            required_results = {
                command.id: by_command.get(command.id) for command in relevant if command.required
            }
            if any(
                result is not None and result.outcome == "failed"
                for result in required_results.values()
            ):
                outcome: VerificationOutcome = "failed"
                summary = "At least one approved command failed"
            elif criterion.manual_review:
                outcome = "manual_review_required"
                summary = "This criterion requires human review"
            elif not results or any(
                result is None or result.outcome == "unverified"
                for result in required_results.values()
            ):
                outcome = "unverified"
                summary = "Required verification evidence is missing or unavailable"
            else:
                outcome = "passed"
                summary = "All mapped approved commands passed"
            criteria.append(CriterionEvidence(criterion.id, outcome, summary, command_ids))
        outcome = _aggregate(tuple(criteria))
        payload = {
            "source_revision": source_revision,
            "manifest_revision": manifest.revision,
            "manifest_digest": manifest.digest,
            "outcome": outcome,
            "commands": [_command_payload(item) for item in commands],
            "criteria": [_criterion_payload(item) for item in criteria],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return VerificationResult(
            source_revision,
            manifest.revision,
            manifest.digest,
            outcome,
            tuple(commands),
            tuple(criteria),
            digest,
        )


async def _git(*args: str, timeout_seconds: int = 120) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
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
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=max(1, timeout_seconds)
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise VerificationUnavailable("Verification source checkout exceeded its time limit")
    if process.returncode:
        raise VerificationPolicyError(
            "Could not materialize the exact verification source revision"
        )
    return stdout.decode("utf-8", errors="replace")


def _aggregate(criteria: tuple[CriterionEvidence, ...]) -> VerificationOutcome:
    outcomes = {item.outcome for item in criteria}
    if "failed" in outcomes:
        return "failed"
    if "unverified" in outcomes:
        return "unverified"
    if "manual_review_required" in outcomes:
        return "manual_review_required"
    return "passed"


def _command_payload(item: CommandEvidence) -> dict[str, object]:
    return {
        "command_id": item.command_id,
        "image": item.image,
        "exit_code": item.exit_code,
        "timed_out": item.timed_out,
        "stdout": item.stdout,
        "stderr": item.stderr,
        "duration_ms": item.duration_ms,
        "outcome": item.outcome,
    }


def _criterion_payload(item: CriterionEvidence) -> dict[str, object]:
    return {
        "criterion_id": item.criterion_id,
        "outcome": item.outcome,
        "summary": item.summary,
        "command_ids": list(item.command_ids),
    }
