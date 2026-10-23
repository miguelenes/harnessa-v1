from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from harnessa.adapters import verification
from harnessa.adapters.verification import (
    PodmanCommandExecutor,
    VerificationCommand,
    VerificationUnavailable,
)

_IMAGE = "localhost/harnessa-verifier@sha256:" + "a" * 64


def _repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Volume test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "volume@test.invalid"], check=True
    )
    (path / "answer.txt").write_text("approved\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "answer.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--quiet", "-m", "fixture"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _command(*, artifacts: tuple[str, ...] = ()) -> VerificationCommand:
    return VerificationCommand(
        id="volume-check",
        argv=("/bin/sh", "-c", "test -f answer.txt"),
        image=_IMAGE,
        criteria=("answer",),
        artifact_paths=artifacts,
    )


def test_volume_command_rejects_untrusted_volume_and_fixture_paths(tmp_path: Path) -> None:
    executor = PodmanCommandExecutor()
    with pytest.raises(verification.VerificationPolicyError, match="volume name is invalid"):
        executor.volume_command(_command(), "not-a-harnessa-volume")

    bad_fixture = tmp_path / "fixture:unsupported"
    bad_fixture.mkdir()
    with pytest.raises(verification.VerificationPolicyError, match="fixture path has unsupported"):
        executor.volume_command(
            _command(), "harnessa-verify-" + "a" * 32, fixture_path=bad_fixture
        )


class _Process:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout_bytes = stdout
        self.stderr_bytes = stderr
        self.returncode: int | None = returncode
        self.stdout = object()
        self.stderr = object()
        self.killed = False

    async def communicate(self, _input=None):
        return self.stdout_bytes, self.stderr_bytes

    async def wait(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _TimedOutProcess(_Process):
    def __init__(self):
        super().__init__(returncode=None)
        self.waits = 0

    async def wait(self):
        self.waits += 1
        if self.waits == 1:
            raise TimeoutError()
        self.returncode = -9
        return self.returncode


@pytest.mark.anyio
async def test_bounded_volume_execution_captures_fixtures_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    revision = _repository(repository)
    artifact = b"test report"
    capture = {
        "records": [
            {
                "path": "report.txt",
                "status": "captured",
                "size_bytes": len(artifact),
                "sha256": "d" * 64,
                "content_base64": base64.b64encode(artifact).decode(),
            }
        ],
        "bytes": len(artifact),
        "missing": False,
    }
    processes = [
        _Process(),
        _Process(),
        _Process(),
        _Process(stdout=json.dumps(capture).encode()),
        _Process(),
        _Process(),
        _Process(),
    ]
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return processes.pop(0)

    async def read_bounded(stream, _limit):
        return b"passed" if stream is not None and len(calls) == 3 else b""

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(verification, "_read_bounded", read_bounded)
    evidence, artifacts, artifact_bytes, missing = await PodmanCommandExecutor(
        workspace_bytes=32 * 1024**2
    ).execute_revision(
        _command(artifacts=("report.txt",)),
        repository_path=repository,
        source_revision=revision,
        protected_fixtures={"expected/answer.txt": b"approved\n"},
    )

    assert evidence.outcome == "passed"
    assert artifacts[0]["content_base64"] == base64.b64encode(artifact).decode()
    assert artifact_bytes == len(artifact)
    assert not missing
    assert any("o=size=33554432,nodev,nosuid" in str(arg) for arg in calls[0])
    assert any("/verification-fixtures:ro" in str(arg) for arg in calls[2])
    assert "--name" in calls[2]
    assert calls[-1][1:3] == ("volume", "rm")
    assert processes == []


@pytest.mark.anyio
async def test_bounded_volume_checkout_failure_is_unverified_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    revision = _repository(repository)
    processes = [
        _Process(),
        _Process(returncode=23),
        _Process(),
        _Process(),
        _Process(),
    ]
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(VerificationUnavailable, match="within the verifier workspace limit"):
        await PodmanCommandExecutor(workspace_bytes=16 * 1024**2).execute_revision(
            _command(),
            repository_path=repository,
            source_revision=revision,
            protected_fixtures={},
        )
    assert calls[-1][1:3] == ("volume", "rm")


@pytest.mark.anyio
async def test_bounded_volume_creation_failure_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    revision = _repository(repository)
    processes = [_Process(returncode=23), _Process(), _Process(returncode=1)]
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return processes.pop(0)

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(VerificationUnavailable, match="Could not allocate"):
        await PodmanCommandExecutor().execute_revision(
            _command(),
            repository_path=repository,
            source_revision=revision,
            protected_fixtures={},
        )
    assert calls[-1][1:3] == ("volume", "exists")


@pytest.mark.anyio
async def test_bounded_volume_requires_full_source_revision(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    _repository(repository)
    with pytest.raises(verification.VerificationPolicyError, match="exact full source"):
        await PodmanCommandExecutor().execute_revision(
            _command(),
            repository_path=repository,
            source_revision="short",
            protected_fixtures={},
        )


@pytest.mark.anyio
async def test_bounded_volume_command_timeout_removes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    revision = _repository(repository)
    command_process = _TimedOutProcess()
    processes = [
        _Process(),
        _Process(),
        command_process,
        _Process(),
        _Process(),
        _Process(),
    ]
    calls: list[tuple[object, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return processes.pop(0)

    async def read_bounded(_stream, _limit):
        return b""

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(verification, "_read_bounded", read_bounded)
    evidence, _, _, _ = await PodmanCommandExecutor(workspace_bytes=16 * 1024**2).execute_revision(
        _command(),
        repository_path=repository,
        source_revision=revision,
        protected_fixtures={},
    )
    assert evidence.outcome == "failed"
    assert evidence.timed_out
    assert command_process.killed
    assert calls[-1][1:3] == ("volume", "rm")


@pytest.mark.anyio
async def test_bounded_volume_removal_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "source"
    revision = _repository(repository)
    processes = [
        _Process(),
        _Process(),
        _Process(),
        _Process(),
        _Process(),
        _Process(returncode=23),
    ]

    async def fake_exec(*_args, **_kwargs):
        return processes.pop(0)

    async def read_bounded(_stream, _limit):
        return b""

    monkeypatch.setattr(verification.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(verification, "_read_bounded", read_bounded)
    with pytest.raises(VerificationUnavailable, match="Could not remove bounded verifier workspace"):
        await PodmanCommandExecutor().execute_revision(
            _command(),
            repository_path=repository,
            source_revision=revision,
            protected_fixtures={},
        )
