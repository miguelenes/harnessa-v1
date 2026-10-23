from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import pytest
from harness2.core.schedule import Namespace
from harness2.substrate.opencode import OpencodeSubstrate
from harnessa.adapters.async_bridge import call_on_event_loop
from harnessa.services.harness_adaptation import FrozenExperiment
from harnessa.services.harness_experiments import (
    HarnessACPTaskAdapter,
    HarnessExperimentWorker,
    _assert_holdout_free_tree,
    _probe_label,
)


class _ComposedAdapter:
    def __init__(self, base: Path) -> None:
        self.substrate = OpencodeSubstrate()
        self._base = base
        self.executed: list[tuple[str, Path, str]] = []
        self.records: list[dict[str, object]] = []

    def base_harness(self) -> Path:
        return self._base

    def execute(self, task_id: str, harness_dir: Path, run_id: str) -> Path:
        self.executed.append((task_id, harness_dir, run_id))
        run_dir = self._base.parent / "composed-result"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        return run_dir.is_dir()


@pytest.mark.anyio
async def test_composition_reruns_the_selected_harness_in_an_isolated_execution(tmp_path: Path) -> None:
    experiment_id = uuid4()
    task_id = uuid4()
    frozen = FrozenExperiment(
        experiment_id=experiment_id,
        task_id=task_id,
        project_id=uuid4(),
        organization_id=uuid4(),
        base_sha="a" * 40,
        specification_hash="b" * 64,
        specification_revision=2,
        policy_revision=3,
        mode="parallel",
        k=1,
        route_alias="probe-route",
        image_revision="sha256:agent",
        tool_revision="upstream-revision",
        root=tmp_path,
        claim_attempt=4,
    )
    mode_dir = Namespace(
        "harnessa", "oc", "agg", 1, tag=f"{experiment_id}-{frozen.claim_attempt}"
    ).task_dir(tmp_path / "runs", str(task_id))
    selected_harness = mode_dir / "harnesses" / "agg1"
    selected_harness.mkdir(parents=True)
    (selected_harness / "systemprompt.md").write_text("selected candidate", encoding="utf-8")
    base_harness = tmp_path / "base-harness"
    base_harness.mkdir()
    (base_harness / "systemprompt.md").write_text("common baseline", encoding="utf-8")
    adapter = _ComposedAdapter(base_harness)
    result: dict[str, Any] = {"selections": {"agg": {"chosen_label": "agg1"}}}
    worker = HarnessExperimentWorker(None)

    await worker._execute_composed(frozen, adapter, result)  # type: ignore[arg-type]

    assert result["composed_successful"] is True
    assert len(adapter.executed) == 1
    executed_task, executed_harness, run_id = adapter.executed[0]
    assert executed_task == str(task_id)
    assert executed_harness == selected_harness
    assert run_id.startswith("composed-agg-")
    assert result["composed"]["agg"]["delivered"] is True


@pytest.mark.anyio
async def test_composition_fails_closed_when_upstream_has_no_selected_harness(tmp_path: Path) -> None:
    frozen = FrozenExperiment(
        experiment_id=uuid4(),
        task_id=uuid4(),
        project_id=uuid4(),
        organization_id=uuid4(),
        base_sha="a" * 40,
        specification_hash="b" * 64,
        specification_revision=1,
        policy_revision=1,
        mode="parallel",
        k=1,
        route_alias="probe-route",
        image_revision="sha256:agent",
        tool_revision="upstream-revision",
        root=tmp_path,
        claim_attempt=1,
    )
    base_harness = tmp_path / "base-harness"
    base_harness.mkdir()
    (base_harness / "systemprompt.md").write_text("baseline", encoding="utf-8")
    adapter = _ComposedAdapter(base_harness)
    worker = HarnessExperimentWorker(None)
    result: dict[str, Any] = {"selections": {"agg": {"chosen_label": ""}}}

    await worker._execute_composed(frozen, adapter, result)  # type: ignore[arg-type]

    assert result["composed_successful"] is False
    assert "composed" not in result
    assert adapter.executed == []


@pytest.mark.anyio
async def test_unavailable_execution_does_not_consume_queued_experiments() -> None:
    worker = HarnessExperimentWorker(None)

    async def unexpected_claim() -> None:
        raise AssertionError("worker must leave the durable queue untouched")

    worker._claim = unexpected_claim  # type: ignore[method-assign]

    assert await worker.run_once() is False


@pytest.mark.anyio
async def test_sync_harness_callback_returns_coroutine_to_owning_loop() -> None:
    owner_loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()

    async def record_thread() -> int:
        return threading.get_ident()

    observed_thread = await asyncio.to_thread(call_on_event_loop, record_thread(), owner_loop)

    assert observed_thread == owner_thread


@pytest.mark.anyio
async def test_probe_trace_records_behavior_shape_without_message_content() -> None:
    adapter = HarnessACPTaskAdapter.__new__(HarnessACPTaskAdapter)
    adapter.records = []
    adapter._records_lock = threading.Lock()
    adapter._events_by_run = {}
    run_id = uuid4()

    await adapter._event_sink(run_id)(
        {"kind": "tool_started", "tool_name": "execute", "text": "private command content"}
    )

    events = adapter._events_by_run[run_id]
    assert events[0]["kind"] == "tool_started"
    assert events[0]["tool_name"] == "execute"
    assert events[0]["text_chars"] == len("private command content")
    assert "private command content" not in str(events)


@pytest.mark.anyio
async def test_stale_attempt_cannot_persist_canonical_probe_evidence(tmp_path: Path) -> None:
    experiment_id = uuid4()
    task_id = uuid4()
    project_id = uuid4()
    frozen = FrozenExperiment(
        experiment_id=experiment_id,
        task_id=task_id,
        project_id=project_id,
        organization_id=uuid4(),
        base_sha="a" * 40,
        specification_hash="b" * 64,
        specification_revision=2,
        policy_revision=3,
        mode="parallel",
        k=1,
        route_alias="probe-route",
        image_revision="sha256:agent",
        tool_revision="upstream-revision",
        root=tmp_path,
        claim_attempt=4,
    )
    stale_experiment = type(
        "StaleExperiment",
        (),
        {"id": experiment_id, "status": "running", "attempts": 5, "cancel_requested_at": None},
    )()

    class Session:
        committed = False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def scalar(self, _statement: object) -> object:
            return stale_experiment

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    worker = HarnessExperimentWorker(None, sessions=lambda: session)  # type: ignore[arg-type]

    await worker._persist(frozen, object(), {})  # type: ignore[arg-type]

    assert session.committed is False


@pytest.mark.anyio
async def test_finish_honors_cancellation_committed_after_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harnessa.services import harness_experiments as module

    experiment_id = uuid4()
    row = type(
        "CancelledExperiment",
        (),
        {
            "id": experiment_id,
            "task_id": uuid4(),
            "status": "running",
            "attempts": 4,
            "cancel_requested_at": None,
            "phase": "baseline_candidate_composed",
            "last_error": None,
            "completed_at": None,
            "result": {"status": "succeeded", "composed_successful": True},
            "result_digest": None,
            "result_storage_key": None,
            "result_size_bytes": None,
        },
    )()
    row.cancel_requested_at = object()  # cancellation committed while persistence held the row lock

    class Session:
        committed = False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def scalar(self, _statement: object) -> object:
            return row

        async def get(self, *_: object) -> None:
            return None

        async def scalars(self, _statement: object) -> list[object]:
            return []

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    monkeypatch.setattr(module.settings, "artifact_root", str(tmp_path))
    worker = HarnessExperimentWorker(None, sessions=lambda: session)  # type: ignore[arg-type]

    await worker._finish(experiment_id, 4, "succeeded", None)

    assert row.status == "cancelled"
    assert row.phase == "cancelled"
    assert row.last_error == "Cancellation was accepted before experiment finalization"
    assert row.result["status"] == "cancelled"
    assert row.result["composed_successful"] is False
    assert row.result_digest
    assert session.committed is True


def test_probe_record_labels_are_bounded_and_distinct() -> None:
    base = "harnessa-oc-agg1-experiment-tag/" + str(uuid4()) + "/"
    baseline_label = _probe_label(base + "base1")
    candidate_label = _probe_label(base + "agg1")

    assert len(baseline_label) <= 100
    assert len(candidate_label) <= 100
    assert baseline_label != candidate_label


def test_experiment_preflight_rejects_holdout_paths_in_frozen_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "visible.py").write_text("print('visible')\n", encoding="utf-8")
    restricted = repo / "hidden_evaluation" / "answers.json"
    restricted.parent.mkdir()
    restricted.write_text('{"answer": "secret"}\n', encoding="utf-8")
    (repo / "answers.json").write_text('{"answer": "secret"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    with pytest.raises(RuntimeError, match="restricted evaluation material"):
        _assert_holdout_free_tree(repo, revision)
