from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest
from harnessa.adapters.acp_execution import ACPExecutionAdapter, _load_owner_id
from harnessa.adapters.execution import ExecutionFailure, RunInput, TaskInput
from harnessa.adapters.podman_sandbox import SandboxIdentity

IMAGE = "ghcr.io/anomalyco/opencode:2.0.32@sha256:" + "a" * 64


def _adapter(tmp_path: Path, *, enabled: bool = True) -> ACPExecutionAdapter:
    return ACPExecutionAdapter(
        enabled=enabled,
        repository_sources={"project": tmp_path},
        workspace_root=tmp_path / "workspaces",
        state_root=tmp_path / "state",
        artifact_store=object(),  # type: ignore[arg-type]
        image=IMAGE,
        owner_id="owner",
        model_route_ready=False,
    )


def test_installation_owner_id_is_persisted_and_invalid_values_are_replaced(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    owner_id = _load_owner_id(state_root)
    assert owner_id == _load_owner_id(state_root)
    assert (state_root / "installation-id").stat().st_mode & 0o777 == 0o600
    (state_root / "installation-id").write_text("not-a-uuid", encoding="ascii")
    replacement = _load_owner_id(state_root)
    assert replacement != "not-a-uuid"
    assert replacement == _load_owner_id(state_root)


def test_adapter_is_disabled_until_all_runtime_requirements_are_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(tmp_path)
    assert adapter.available is False
    adapter.model_route_ready = True
    adapter.model_router = object()  # type: ignore[assignment]
    adapter._validated = True
    monkeypatch.setattr("harnessa.adapters.acp_execution.shutil.which", lambda _: "/bin/podman")
    assert adapter.available is True
    adapter.repository_sources.clear()
    assert adapter.available is False


@pytest.mark.anyio
async def test_validate_records_health_and_fails_closed(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)

    class Backend:
        failure: Exception | None = None

        async def validate(self) -> None:
            if self.failure:
                raise self.failure

    backend = Backend()
    adapter.backend = backend  # type: ignore[assignment]
    await adapter.validate()
    assert adapter.sandbox_validated is True
    assert adapter._validated_at > 0
    backend.failure = RuntimeError("rootless validation failed")
    with pytest.raises(RuntimeError, match="rootless validation failed"):
        await adapter.validate()
    assert adapter.sandbox_validated is False


@pytest.mark.anyio
async def test_reconcile_orphans_removes_only_owned_stale_fences(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    run_id = uuid4()

    class Backend:
        async def owned_containers(self) -> list[dict[str, Any]]:
            return [
                {"Id": "current", "Labels": {"harnessa.owner": "owner", "harnessa.run_id": str(run_id), "harnessa.fencing_token": "3"}},
                {"Id": "stale", "Labels": f"harnessa.owner=owner,harnessa.run_id={run_id},harnessa.fencing_token=2"},
                {"Id": "foreign", "Labels": {"harnessa.owner": "someone-else"}},
                {"Id": "malformed", "Labels": {"harnessa.owner": "owner", "harnessa.run_id": "broken", "harnessa.fencing_token": "x"}},
                {"Id": "missing-owner", "Labels": None},
            ]

        async def remove_owned(self, container_id: str) -> None:
            removed.append(container_id)

    removed: list[str] = []
    adapter.backend = Backend()  # type: ignore[assignment]

    async def current_fence(candidate: object, fence: int) -> bool:
        return candidate == run_id and fence == 3

    assert await adapter.reconcile_orphans(is_current_fence=current_fence) == 1  # type: ignore[arg-type]
    assert removed == ["stale"]


@pytest.mark.anyio
async def test_cancel_signals_session_and_stops_owned_process(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    run_id = uuid4()
    identity = SandboxIdentity("owner", str(run_id), str(uuid4()), 1, 7)
    events: list[str] = []

    class Session:
        async def cancel(self) -> None:
            events.append("session")

    class Process:
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("terminate")

    class Backend:
        async def stop_and_remove(self, actual: SandboxIdentity) -> None:
            assert actual == identity
            events.append("container")

    adapter.backend = Backend()  # type: ignore[assignment]
    adapter._active[run_id] = (Session(), identity, Process())  # type: ignore[arg-type]
    await adapter.cancel(run_id)
    await adapter.cancel(uuid4())
    assert events == ["session", "container", "terminate"]


@pytest.mark.anyio
async def test_watchdog_terminates_when_workspace_limit_is_exceeded(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    workspace = object()
    limit_hit = asyncio.Event()
    terminated: list[bool] = []

    class Workspaces:
        async def size_bytes(self, _: object) -> int:
            return adapter.limits.workspace_bytes + 1

    class Process:
        returncode: int | None = None

        def terminate(self) -> None:
            terminated.append(True)

    adapter.workspaces = Workspaces()  # type: ignore[assignment]
    await adapter._workspace_watchdog(workspace, Process(), limit_hit)  # type: ignore[arg-type]
    assert limit_hit.is_set()
    assert terminated == [True]


def test_checkpoint_summaries_retain_harnessa_fencing_and_policy(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    task_id = uuid4()
    project_id = uuid4()
    run_id = uuid4()
    run = RunInput(run_id, 2, 9)
    identity = SandboxIdentity("owner", str(run_id), str(task_id), 2, 9)
    workspace = type(
        "Workspace",
        (),
        {
            "project_id": project_id,
            "base_commit": "b" * 40,
            "branch": "harnessa/test",
        },
    )()
    checkpoint = adapter._checkpoint(
        workspace,  # type: ignore[arg-type]
        identity,
        "prompt_running",
        datetime.now(UTC),
        2_000_000_000,
        capabilities={"fs": True},
        agent_info={"version": "2.0.32", "private_key": "redact"},
        progress_counts={"tool_call": 2},
    )
    summary = adapter._workspace_summary(workspace, identity, 2_000_000_000)  # type: ignore[arg-type]
    assert checkpoint["run_id"] == str(run.id)
    assert checkpoint["fencing_token"] == 9
    assert checkpoint["capabilities"] == {"fs": True}
    assert "private_key" not in checkpoint["agent_info"]
    assert summary["project_id"] == str(project_id)
    assert summary["egress_grants"] == []


@pytest.mark.anyio
async def test_execute_completes_through_fake_acp_and_cleans_owned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harnessa.adapters.acp_execution as module

    task = TaskInput(uuid4(), uuid4(), "Implement feature", "Use the bounded test fixture")
    run = RunInput(uuid4(), 1, 4)
    workspace = SimpleNamespace(
        task_id=task.id,
        project_id=task.project_id,
        run_id=run.id,
        attempt=run.attempt,
        fencing_token=run.fencing_token,
        repository_path=tmp_path,
        workspace_path=tmp_path / "workspace",
        state_path=tmp_path / "state.json",
        base_commit="c" * 40,
        branch="harnessa/test",
        created_at=datetime.now(UTC),
    )
    workspace.workspace_path.mkdir()
    completed: list[str] = []
    checkpoints: list[dict[str, Any]] = []

    class DB:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, key: object) -> object:
            assert key == task.project_id
            return SimpleNamespace(organization_id=uuid4())

        async def scalar(self, _: object) -> None:
            return None

    class Workspaces:
        async def provision(self, *_: object, **__: object) -> object:
            return workspace

        async def mark_status(self, _: object, status: str) -> None:
            completed.append(f"status:{status}")

        async def changed_files(self, _: object) -> list[dict[str, str]]:
            return [{"path": "README.md", "status": "modified"}]

        async def patch(self, _: object, __: int) -> bytes:
            return b"diff --git a/README.md b/README.md\n"

        async def size_bytes(self, _: object) -> int:
            return 1

        async def remove_workspace(self, _: object) -> None:
            completed.append("workspace:removed")

    class Backend:
        executable = "podman"
        image = IMAGE

        async def validate(self) -> None:
            return None

        async def build_model_relay(self, path: Path) -> Path:
            return path

        async def launch(self, **_: object) -> Process:
            return Process()

        async def stop_and_remove(self, _: SandboxIdentity) -> None:
            completed.append("container:removed")

    class Process:
        returncode = 0

        def terminate(self) -> None:
            completed.append("process:terminated")

        async def wait(self) -> int:
            return 0

    class Broker:
        socket_path = tmp_path / "model.sock"

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            completed.append("broker:closed")

    class Session:
        def __init__(self, *_: object, **__: object) -> None:
            self.client = SimpleNamespace(update_counts={"tool_call": 1})

        async def initialize(self) -> object:
            return SimpleNamespace(
                agent_capabilities=None,
                agent_info=SimpleNamespace(model_dump=lambda **__: {"version": "2.0.32"}),
                protocol_version="1",
            )

        async def new_session(self, **_: object) -> None:
            return None

        async def prompt(self, _: str, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            return "end_turn"

        async def close(self) -> None:
            completed.append("session:closed")

    class Agent:
        agent_id = "opencode"
        command = ("opencode", "acp")

    adapter = _adapter(tmp_path)
    adapter.repository_sources[str(task.project_id)] = tmp_path
    adapter.enabled = True
    adapter.model_route_ready = True
    adapter.model_router = object()  # type: ignore[assignment]
    adapter.model_alias = "budgeted"
    adapter.agent = Agent()  # type: ignore[assignment]
    adapter.backend = Backend()  # type: ignore[assignment]
    adapter.workspaces = Workspaces()  # type: ignore[assignment]
    adapter.sessions_factory = lambda: DB()  # type: ignore[assignment]
    monkeypatch.setattr(module, "ACPModelBroker", lambda **_: Broker())
    monkeypatch.setattr(module, "ACPAgentSession", Session)

    async def save_checkpoint(value: object) -> None:
        checkpoints.append(dict(value))  # type: ignore[arg-type]

    async def not_cancelled() -> bool:
        return False

    async def current_fence(_: object, __: int) -> bool:
        return True

    async def publish(patch: bytes, content_type: str) -> dict[str, object]:
        assert patch.startswith(b"diff --git")
        assert content_type == "application/vnd.git-patch"
        return {"artifact_id": "fixture"}

    result = await adapter.execute(
        task,
        run,
        checkpoint=None,
        save_checkpoint=save_checkpoint,
        is_cancelled=not_cancelled,
        is_current_fence=current_fence,  # type: ignore[arg-type]
        publish_artifact=publish,
    )

    assert result["status"] == "completed"
    assert result["agent_version"] == "2.0.32"
    assert result["patch_artifact"] == {"artifact_id": "fixture"}
    assert checkpoints[0]["fencing_token"] == run.fencing_token
    assert [event for event in completed if event.endswith("removed")] == [
        "container:removed",
        "workspace:removed",
    ]
    assert "broker:closed" in completed
    assert "session:closed" in completed


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model_route_ready", "repository_configured", "expected_code"),
    [
        (False, True, "model_route_unavailable"),
        (True, False, "repository_source_not_configured"),
    ],
)
async def test_execute_fails_closed_before_side_effects(
    tmp_path: Path,
    model_route_ready: bool,
    repository_configured: bool,
    expected_code: str,
) -> None:
    adapter = _adapter(tmp_path)
    adapter.model_route_ready = model_route_ready
    adapter.model_router = object() if model_route_ready else None  # type: ignore[assignment]
    task = TaskInput(uuid4(), uuid4(), "Task", "Description")
    if repository_configured:
        adapter.repository_sources[str(task.project_id)] = tmp_path
    run = RunInput(uuid4(), 1, 1)

    async def unused(*_: object) -> None:
        raise AssertionError("precondition failure should not perform execution effects")

    with pytest.raises(ExecutionFailure) as failure:
        await adapter.execute(
            task,
            run,
            checkpoint=None,
            save_checkpoint=unused,  # type: ignore[arg-type]
            is_cancelled=lambda: _bool(False),
            is_current_fence=lambda *_: _bool(True),
            publish_artifact=unused,  # type: ignore[arg-type]
        )
    assert failure.value.code == expected_code


@pytest.mark.anyio
async def test_execute_rejects_deleted_project_and_still_cleans_owned_identity(
    tmp_path: Path,
) -> None:
    from contextlib import asynccontextmanager

    task = TaskInput(uuid4(), uuid4(), "Task", "Description")
    run = RunInput(uuid4(), 1, 6)
    events: list[str] = []

    class Backend:
        image = IMAGE
        executable = "podman"

        async def validate(self) -> None:
            return None

        async def stop_and_remove(self, identity: SandboxIdentity) -> None:
            assert identity.run_id == str(run.id)
            events.append("container-cleaned")

    @asynccontextmanager
    async def sessions():
        class DB:
            async def get(self, *_: object) -> None:
                return None

        yield DB()

    adapter = _adapter(tmp_path)
    adapter.repository_sources[str(task.project_id)] = tmp_path
    adapter.model_route_ready = True
    adapter.model_router = object()  # type: ignore[assignment]
    adapter.backend = Backend()  # type: ignore[assignment]
    adapter.sessions_factory = sessions  # type: ignore[assignment]

    async def unused(*_: object) -> None:
        return None

    with pytest.raises(ExecutionFailure) as failure:
        await adapter.execute(
            task,
            run,
            checkpoint=None,
            save_checkpoint=unused,  # type: ignore[arg-type]
            is_cancelled=lambda: _bool(False),
            is_current_fence=lambda *_: _bool(True),
            publish_artifact=unused,  # type: ignore[arg-type]
        )
    assert failure.value.code == "project_not_found"
    assert events == ["container-cleaned"]


@pytest.mark.anyio
async def test_execute_returns_cancelled_before_launch_and_preserves_workspace(
    tmp_path: Path,
) -> None:
    from contextlib import asynccontextmanager

    task = TaskInput(uuid4(), uuid4(), "Task", "Description")
    run = RunInput(uuid4(), 1, 8)
    workspace = SimpleNamespace(
        workspace_path=tmp_path / "workspace",
        base_commit="d" * 40,
        branch="harnessa/test",
        project_id=task.project_id,
    )
    statuses: list[str] = []
    launched = False

    class DB:
        async def get(self, *_: object) -> object:
            return SimpleNamespace(organization_id=uuid4())

        async def scalar(self, *_: object) -> None:
            return None

    @asynccontextmanager
    async def sessions():
        yield DB()

    class Workspaces:
        async def provision(self, *_: object, **__: object) -> object:
            return workspace

        async def mark_status(self, _: object, status: str) -> None:
            statuses.append(status)

    class Backend:
        image = IMAGE
        executable = "podman"

        async def validate(self) -> None:
            return None

        async def stop_and_remove(self, _: SandboxIdentity) -> None:
            statuses.append("container-removed")

        async def launch(self, **_: object) -> object:
            nonlocal launched
            launched = True
            raise AssertionError("cancelled run must not launch the agent")

    adapter = _adapter(tmp_path)
    adapter.repository_sources[str(task.project_id)] = tmp_path
    adapter.model_route_ready = True
    adapter.model_router = object()  # type: ignore[assignment]
    adapter.sessions_factory = sessions  # type: ignore[assignment]
    adapter.workspaces = Workspaces()  # type: ignore[assignment]
    adapter.backend = Backend()  # type: ignore[assignment]
    saved: list[object] = []

    async def save(value: object) -> None:
        saved.append(value)

    result = await adapter.execute(
        task,
        run,
        checkpoint=None,
        save_checkpoint=save,
        is_cancelled=lambda: _bool(True),
        is_current_fence=lambda *_: _bool(True),
        publish_artifact=lambda *_: _published(),
    )
    assert result["status"] == "cancelled"
    assert saved and saved[0]["phase"] == "session_starting"  # type: ignore[index]
    assert statuses == ["running", "container-removed", "preserved_for_recovery"]
    assert not launched


async def _bool(value: bool) -> bool:
    return value


async def _published() -> dict[str, object]:
    return {}
