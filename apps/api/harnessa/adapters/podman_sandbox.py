"""Rootless Podman isolation for ACP agents; container control stays trusted-side."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from subprocess import CompletedProcess

from harnessa.adapters.sandbox_policy import SandboxLimits, SandboxPolicyError

_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class SandboxUnavailable(RuntimeError):
    """Rootless Podman or the exact agent image is not ready."""


class SandboxBackend(Protocol):
    """Trusted host-side boundary for a future, separately verified Docker backend."""

    image: str
    owner_id: str
    executable: str

    async def validate(self) -> None: ...

    async def launch(
        self,
        *,
        identity: SandboxIdentity,
        workspace: Path,
        state_root: Path,
        agent_command: tuple[str, ...],
        max_frame_bytes: int,
        broker_socket: Path | None = None,
        opencode_config: Path | None = None,
        relay_binary: Path | None = None,
    ) -> asyncio.subprocess.Process: ...

    async def build_model_relay(self, destination: Path) -> Path: ...

    async def stop_and_remove(self, identity: SandboxIdentity, *, timeout: int = 2) -> None: ...

    async def owned_containers(self) -> list[dict[str, Any]]: ...

    async def remove_owned(self, container_id: str) -> None: ...


@dataclass(frozen=True)
class SandboxIdentity:
    owner_id: str
    run_id: str
    task_id: str
    attempt: int
    fencing_token: int

    @property
    def container_name(self) -> str:
        return f"harnessa-acp-{self.run_id.replace('-', '')[:16]}-f{self.fencing_token}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "harnessa.managed": "true",
            "harnessa.owner": self.owner_id,
            "harnessa.run_id": self.run_id,
            "harnessa.task_id": self.task_id,
            "harnessa.attempt": str(self.attempt),
            "harnessa.fencing_token": str(self.fencing_token),
        }


class PodmanSandboxBackend:
    def __init__(
        self,
        *,
        image: str,
        owner_id: str,
        limits: SandboxLimits | None = None,
        executable: str = "podman",
        platform: str | None = None,
    ) -> None:
        if not _DIGEST.fullmatch(image):
            raise SandboxPolicyError("ACP agent images must be pinned by sha256 digest")
        self.image = image
        self.owner_id = owner_id
        self.limits = limits or SandboxLimits()
        self.executable = shutil.which(executable) or executable
        self.platform = platform

    async def validate(self) -> None:
        if not shutil.which(self.executable) and not Path(self.executable).is_file():
            raise SandboxUnavailable("Podman executable is unavailable")
        info = await self._run("info", "--format", "json")
        try:
            rootless = bool(json.loads(info.stdout)["host"]["security"]["rootless"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SandboxUnavailable("Could not verify Podman rootless mode") from exc
        if not rootless:
            raise SandboxUnavailable("ACP execution requires rootless Podman")
        result = await self._run("image", "exists", self.image, check=False)
        if result.returncode:
            raise SandboxUnavailable(
                "The exact digest-pinned ACP agent image is not present locally"
            )

    def command(
        self,
        *,
        identity: SandboxIdentity,
        workspace: Path,
        state_root: Path,
        agent_command: tuple[str, ...],
        broker_socket: Path | None = None,
        opencode_config: Path | None = None,
        relay_binary: Path | None = None,
    ) -> list[str]:
        workspace = workspace.resolve(strict=True)
        state_root = state_root.resolve(strict=True)
        if any(character in str(workspace) for character in ",:"):
            raise SandboxPolicyError("Managed workspace path may not contain ':' or ','")
        broker_mounts = (broker_socket, opencode_config, relay_binary)
        if any(path is not None for path in broker_mounts) and not all(
            path is not None for path in broker_mounts
        ):
            raise SandboxPolicyError("The model broker requires its socket, config, and relay")
        cidfile = state_root / f"{identity.container_name}.cid"
        labels = [
            argument
            for key, value in identity.labels.items()
            for argument in ("--label", f"{key}={value}")
        ]
        command = [
            self.executable,
            "run",
            "--interactive",
            "--name",
            identity.container_name,
            "--cidfile",
            str(cidfile),
            "--pull=never",
            "--platform",
            self.platform or _platform_name(),
            *labels,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--userns=keep-id",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            str(self.limits.memory_bytes),
            "--cpus",
            str(self.limits.cpus),
            "--pids-limit",
            str(self.limits.pids),
            "--ulimit",
            "nofile=1024:1024",
            "--ulimit",
            "fsize=1073741824:1073741824",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.limits.tmpfs_bytes},mode=1777",
            "--tmpfs",
            "/run/harnessa:rw,noexec,nosuid,nodev,size=1m,mode=711",
            "--volume",
            f"{workspace}:/workspace:rw,rprivate,Z",
        ]
        if broker_socket is not None and opencode_config is not None and relay_binary is not None:
            socket_path = broker_socket.resolve(strict=True)
            config_path = opencode_config.resolve(strict=True)
            relay_path = relay_binary.resolve(strict=True)
            if not stat.S_ISSOCK(socket_path.stat().st_mode):
                raise SandboxPolicyError("The model broker path is not a Unix socket")
            if any(character in str(path) for path in (socket_path, config_path, relay_path) for character in ",:"):
                raise SandboxPolicyError("Model broker paths may not contain ':' or ','")
            if not relay_path.is_file() or not os.access(relay_path, os.X_OK):
                raise SandboxPolicyError("The model socket relay is not executable")
            command.extend(
                [
                    "--volume",
                    f"{socket_path}:/run/harnessa/model.sock:ro,rprivate,Z",
                    "--volume",
                    f"{config_path}:/run/harnessa/opencode.json:ro,rprivate,Z",
                    "--volume",
                    f"{relay_path}:/harnessa/model-relay:ro,rprivate,Z",
                    "--env",
                    "OPENCODE_CONFIG=/run/harnessa/opencode.json",
                ]
            )
        if broker_socket is not None:
            command.extend(["--entrypoint", "/bin/sh"])
        command.extend(
            [
                "--workdir",
                "/workspace",
                "--env",
                "HOME=/tmp/home",
                "--env",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                self.image,
            ]
        )
        if broker_socket is not None:
            command.extend(
                [
                    "-c",
                    "/harnessa/model-relay /run/harnessa/model.sock 8765 </dev/null >/dev/null 2>&1 & exec opencode \"$@\"",
                    "harnessa-agent",
                    *agent_command,
                ]
            )
        else:
            command.extend(agent_command)
        return command

    async def build_model_relay(self, destination: Path) -> Path:
        compiler = shutil.which("cc")
        if compiler is None:
            raise SandboxUnavailable("A C compiler is required to build the isolated model socket relay")
        source = Path(__file__).with_name("acp_socket_relay.c")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        process = await asyncio.create_subprocess_exec(
            compiler,
            "-static",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pthread",
            str(source),
            "-o",
            str(temporary),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            temporary.unlink(missing_ok=True)
            logger_message = (stderr or stdout).decode("utf-8", errors="replace")[:300]
            raise SandboxUnavailable(f"Could not build the model socket relay: {logger_message}")
        temporary.chmod(0o700)
        temporary.replace(destination)
        return destination

    async def launch(
        self,
        *,
        identity: SandboxIdentity,
        workspace: Path,
        state_root: Path,
        agent_command: tuple[str, ...],
        max_frame_bytes: int,
        broker_socket: Path | None = None,
        opencode_config: Path | None = None,
        relay_binary: Path | None = None,
    ) -> asyncio.subprocess.Process:
        command = self.command(
            identity=identity,
            workspace=workspace,
            state_root=state_root,
            agent_command=agent_command,
            broker_socket=broker_socket,
            opencode_config=opencode_config,
            relay_binary=relay_binary,
        )
        try:
            return await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=max_frame_bytes + 1,
                env=self._podman_environment(),
            )
        except OSError as exc:
            raise SandboxUnavailable("Could not start the rootless ACP sandbox") from exc

    async def stop_and_remove(self, identity: SandboxIdentity, *, timeout: int = 2) -> None:
        result = await self._run(
            "rm", "--force", identity.container_name, check=False, timeout=timeout + 5
        )
        if result.returncode and b"no such container" not in result.stderr.lower():
            raise SandboxUnavailable("Could not remove the owned ACP container")

    async def owned_containers(self) -> list[dict[str, Any]]:
        result = await self._run(
            "ps",
            "--all",
            "--filter",
            "label=harnessa.managed=true",
            "--filter",
            f"label=harnessa.owner={self.owner_id}",
            "--format",
            "json",
        )
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxUnavailable("Podman returned malformed container inventory") from exc
        if not isinstance(rows, list):
            raise SandboxUnavailable("Podman returned an invalid container inventory")
        return [row for row in rows if isinstance(row, dict)]

    async def remove_owned(self, container_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise SandboxPolicyError("Invalid Podman container id")
        await self._run("rm", "--force", container_id)

    async def _run(
        self,
        *args: str,
        check: bool = True,
        timeout: float = 15,
    ) -> CompletedProcess[bytes]:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._podman_environment(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise SandboxUnavailable("Podman control command timed out") from None
        result = CompletedProcess(
            [self.executable, *args], int(process.returncode or 0), stdout, stderr
        )
        if check and result.returncode:
            detail = stderr.decode("utf-8", errors="replace")[:300]
            raise SandboxUnavailable(f"Podman control command failed: {detail}")
        return result

    @staticmethod
    def _podman_environment() -> dict[str, str]:
        """Pass only Podman's rootless runtime hints, never application credentials."""
        values = {"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "C.UTF-8"}
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "CONTAINERS_CONF"):
            value = os.environ.get(key)
            if value:
                values[key] = value
        return values


def _platform_name() -> str:
    import platform

    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if architecture is None:
        raise SandboxPolicyError("No pinned ACP image platform is available for this host")
    return f"linux/{architecture}"
