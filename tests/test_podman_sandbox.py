from __future__ import annotations

import json
import socket
import stat
from pathlib import Path
from subprocess import CompletedProcess
from uuid import uuid4

import pytest
from harnessa.adapters.podman_sandbox import (
    PodmanSandboxBackend,
    SandboxIdentity,
    SandboxUnavailable,
)
from harnessa.adapters.sandbox_policy import SandboxPolicyError

IMAGE = "ghcr.io/anomalyco/opencode:1.19.4@sha256:" + "a" * 64


def _backend() -> PodmanSandboxBackend:
    return PodmanSandboxBackend(image=IMAGE, owner_id="harnessa-test", executable="podman")


def test_container_command_is_networkless_and_uses_owned_labels(tmp_path: Path) -> None:
    backend = PodmanSandboxBackend(image=IMAGE, owner_id="harnessa-test", platform="linux/amd64")
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    identity = SandboxIdentity("harnessa-test", str(uuid4()), str(uuid4()), 2, 9)
    command = backend.command(
        identity=identity,
        workspace=workspace,
        state_root=state,
        agent_command=("opencode", "acp"),
    )
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--pull=never" in command
    assert "linux/amd64" in command
    assert "--label" in command
    assert f"harnessa.owner={identity.owner_id}" in command
    assert command[-2:] == ["opencode", "acp"]


def test_container_command_rejects_partial_broker_mounts(tmp_path: Path) -> None:
    backend = _backend()
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    with pytest.raises(SandboxPolicyError, match="requires its socket, config, and relay"):
        backend.command(
            identity=SandboxIdentity("owner", str(uuid4()), str(uuid4()), 1, 1),
            workspace=workspace,
            state_root=state,
            agent_command=("opencode", "acp"),
            broker_socket=tmp_path / "broker.sock",
        )


def test_container_command_mounts_only_valid_broker_sockets_and_executables(
    tmp_path: Path,
) -> None:
    backend = _backend()
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    model_socket = tmp_path / "model.sock"
    orchestrator_socket = tmp_path / "orchestrator.sock"
    sockets: list[socket.socket] = []
    for path in (model_socket, orchestrator_socket):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        sockets.append(listener)
    config = tmp_path / "opencode.json"
    config.write_text("{}", encoding="utf-8")
    relay = tmp_path / "model-relay"
    relay.write_text("fixture", encoding="utf-8")
    relay.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    tool = tmp_path / "orchestrator-mcp"
    tool.write_text("fixture", encoding="utf-8")
    tool.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    try:
        command = backend.command(
            identity=SandboxIdentity("harnessa-test", str(uuid4()), str(uuid4()), 1, 3),
            workspace=workspace,
            state_root=state,
            agent_command=("opencode", "acp"),
            broker_socket=model_socket,
            opencode_config=config,
            relay_binary=relay,
            orchestrator_socket=orchestrator_socket,
            orchestrator_binary=tool,
        )
        assert f"{model_socket}:/run/harnessa/model.sock:ro,rprivate,Z" in command
        assert f"{orchestrator_socket}:/run/harnessa/orchestrator.sock:ro,rprivate,Z" in command
        assert command[-3:] == ["harnessa-agent", "opencode", "acp"]
        assert "/bin/sh" in command
    finally:
        for listener in sockets:
            listener.close()


def test_container_command_rejects_non_socket_broker_path(tmp_path: Path) -> None:
    backend = _backend()
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    broker_path = tmp_path / "broker.sock"
    broker_path.write_text("not a socket", encoding="utf-8")
    config = tmp_path / "opencode.json"
    config.write_text("{}", encoding="utf-8")
    relay = tmp_path / "relay"
    relay.write_text("fixture", encoding="utf-8")
    relay.chmod(0o700)
    with pytest.raises(SandboxPolicyError, match="not a Unix socket"):
        backend.command(
            identity=SandboxIdentity("owner", str(uuid4()), str(uuid4()), 1, 1),
            workspace=workspace,
            state_root=state,
            agent_command=("opencode", "acp"),
            broker_socket=broker_path,
            opencode_config=config,
            relay_binary=relay,
        )
    with pytest.raises(SandboxPolicyError, match="requires its socket and binary"):
        backend.command(
            identity=SandboxIdentity("owner", str(uuid4()), str(uuid4()), 1, 1),
            workspace=workspace,
            state_root=state,
            agent_command=("opencode", "acp"),
            orchestrator_socket=tmp_path / "orchestrator.sock",
        )


@pytest.mark.anyio
async def test_validate_requires_rootless_mode_and_exact_local_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()
    monkeypatch.setattr("harnessa.adapters.podman_sandbox.shutil.which", lambda _: "/usr/bin/podman")
    responses = [
        CompletedProcess([], 0, b'{"host":{"security":{"rootless":true}}}', b""),
        CompletedProcess([], 0, b"", b""),
    ]

    async def run(*_: str, **__: object) -> CompletedProcess[bytes]:
        return responses.pop(0)

    monkeypatch.setattr(backend, "_run", run)
    await backend.validate()

    async def non_rootless(*_: str, **__: object) -> CompletedProcess[bytes]:
        return CompletedProcess([], 0, b'{"host":{"security":{"rootless":false}}}', b"")

    monkeypatch.setattr(backend, "_run", non_rootless)
    with pytest.raises(SandboxUnavailable, match="requires rootless Podman"):
        await backend.validate()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("info", "image_status", "message"),
    [
        (b"not-json", 0, "Could not verify Podman rootless mode"),
        (b'{"host":{}}', 0, "Could not verify Podman rootless mode"),
        (b'{"host":{"security":{"rootless":true}}}', 1, "exact digest-pinned"),
    ],
)
async def test_validate_rejects_unverifiable_or_missing_podman_state(
    monkeypatch: pytest.MonkeyPatch, info: bytes, image_status: int, message: str
) -> None:
    backend = _backend()
    monkeypatch.setattr("harnessa.adapters.podman_sandbox.shutil.which", lambda _: "/usr/bin/podman")
    responses = [
        CompletedProcess([], 0, info, b""),
        CompletedProcess([], image_status, b"", b"image absent"),
    ]

    async def run(*_: str, **__: object) -> CompletedProcess[bytes]:
        return responses.pop(0)

    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(SandboxUnavailable, match=message):
        await backend.validate()


@pytest.mark.anyio
async def test_owned_inventory_parses_only_objects_and_validates_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()
    async def run(*_args: str, **_kwargs: object) -> CompletedProcess[bytes]:
        return _completed(json.dumps([{"Id": "abc"}, "ignored", {"ID": "def"}]))

    monkeypatch.setattr(backend, "_run", run)
    assert await backend.owned_containers() == [{"Id": "abc"}, {"ID": "def"}]
    with pytest.raises(SandboxPolicyError, match="Invalid Podman container id"):
        await backend.remove_owned("../../not-a-container")


@pytest.mark.anyio
async def test_stop_and_remove_ignores_only_missing_container(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    identity = SandboxIdentity("owner", str(uuid4()), str(uuid4()), 1, 2)
    result = CompletedProcess([], 1, b"", b"no such container")

    async def run(*_: str, **__: object) -> CompletedProcess[bytes]:
        return result

    monkeypatch.setattr(backend, "_run", run)
    await backend.stop_and_remove(identity)
    result = CompletedProcess([], 1, b"", b"permission denied")
    with pytest.raises(SandboxUnavailable, match="Could not remove the owned ACP container"):
        await backend.stop_and_remove(identity)


@pytest.mark.anyio
async def test_native_bridge_builds_use_bounded_compiler_arguments_and_atomic_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend()
    invocations: list[tuple[object, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def compile_fixture(*args: object, **_: object) -> Process:
        invocations.append(args)
        output = Path(str(args[args.index("-o") + 1]))
        output.write_bytes(b"offline compiler fixture")
        return Process()

    monkeypatch.setattr("harnessa.adapters.podman_sandbox.shutil.which", lambda _: "/usr/bin/cc")
    monkeypatch.setattr(
        "harnessa.adapters.podman_sandbox.asyncio.create_subprocess_exec", compile_fixture
    )
    relay = await backend.build_model_relay(tmp_path / "bin" / "model-relay")
    tool = await backend.build_orchestrator_tool(tmp_path / "bin" / "orchestrator-mcp")
    assert relay.read_bytes() == b"offline compiler fixture"
    assert tool.read_bytes() == b"offline compiler fixture"
    assert relay.stat().st_mode & 0o777 == 0o700
    assert tool.stat().st_mode & 0o777 == 0o700
    assert len(invocations) == 2
    assert all("-Werror" in args and "-static" in args for args in invocations)


@pytest.mark.anyio
async def test_native_bridge_build_reports_missing_compiler_and_failed_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend()
    monkeypatch.setattr("harnessa.adapters.podman_sandbox.shutil.which", lambda _: None)
    with pytest.raises(SandboxUnavailable, match="C compiler is required"):
        await backend.build_model_relay(tmp_path / "model-relay")

    class FailedProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"compile failed"

    async def fail_compile(*_: object, **__: object) -> FailedProcess:
        return FailedProcess()

    monkeypatch.setattr("harnessa.adapters.podman_sandbox.shutil.which", lambda _: "/usr/bin/cc")
    monkeypatch.setattr(
        "harnessa.adapters.podman_sandbox.asyncio.create_subprocess_exec", fail_compile
    )
    destination = tmp_path / "orchestrator-mcp"
    with pytest.raises(SandboxUnavailable, match="Could not build the orchestrator MCP tool"):
        await backend.build_orchestrator_tool(destination)
    assert not destination.with_suffix(".tmp").exists()


@pytest.mark.anyio
async def test_podman_control_command_handles_exit_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend()

    class Process:
        returncode = 0

        def __init__(self, stdout: bytes = b"ok", stderr: bytes = b"") -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout, self.stderr

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return 0

    process = Process()

    async def launch_process(*_: object, **__: object) -> Process:
        return process

    monkeypatch.setattr(
        "harnessa.adapters.podman_sandbox.asyncio.create_subprocess_exec", launch_process
    )
    result = await backend._run("info")
    assert result.returncode == 0 and result.stdout == b"ok"

    process.returncode = 3
    process.stderr = b"bounded error detail"
    result = await backend._run("ps", check=False)
    assert result.returncode == 3
    with pytest.raises(SandboxUnavailable, match="Podman control command failed"):
        await backend._run("ps")

    async def timeout(awaitable: object, **_: object) -> object:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise TimeoutError

    monkeypatch.setattr("harnessa.adapters.podman_sandbox.asyncio.wait_for", timeout)
    with pytest.raises(SandboxUnavailable, match="timed out"):
        await backend._run("info")
    assert process.killed


@pytest.mark.anyio
async def test_podman_launch_wraps_os_errors_without_losing_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "command", lambda **_: ["podman", "run"])
    calls: list[tuple[object, ...]] = []

    async def launch(*args: object, **kwargs: object) -> object:
        calls.append(args)
        assert kwargs["limit"] == 1025
        return object()

    monkeypatch.setattr(
        "harnessa.adapters.podman_sandbox.asyncio.create_subprocess_exec", launch
    )
    identity = SandboxIdentity("owner", str(uuid4()), str(uuid4()), 1, 1)
    assert await backend.launch(
        identity=identity,
        workspace=tmp_path,
        state_root=tmp_path,
        agent_command=("opencode", "acp"),
        max_frame_bytes=1024,
    )
    assert calls == [("podman", "run")]

    async def unavailable(*_: object, **__: object) -> object:
        raise OSError("fixture unavailable")

    monkeypatch.setattr(
        "harnessa.adapters.podman_sandbox.asyncio.create_subprocess_exec", unavailable
    )
    with pytest.raises(SandboxUnavailable, match="Could not start the rootless ACP sandbox"):
        await backend.launch(
            identity=identity,
            workspace=tmp_path,
            state_root=tmp_path,
            agent_command=("opencode", "acp"),
            max_frame_bytes=1024,
        )


def _completed(value: str) -> CompletedProcess[bytes]:
    return CompletedProcess([], 0, value.encode(), b"")
