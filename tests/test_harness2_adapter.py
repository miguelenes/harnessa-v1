from __future__ import annotations

import json
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harness2.substrate.opencode import OpencodeSubstrate
from harnessa.adapters.harness2 import (
    COMPONENTS,
    UPSTREAM_LICENSE,
    UPSTREAM_REVISION,
    validate_product_tree,
)
from harnessa.api.harness_routes import (
    _promotion_gates_passed,
    apply_reviewed_experience_transition,
)
from harnessa.services.harness_adaptation import (
    FrozenExperiment,
    GatewayHarness2Proposer,
    HarnessAdaptationError,
    _context_payload,
    _extract_chat_text,
    _parse_edit_response,
    apply_candidate_edits,
    run_upstream_task,
)
from harnessa.services.verification_workflow import _harness_learning_hook


def test_manifest_maps_all_upstream_component_categories() -> None:
    assert {item.category for item in COMPONENTS} == {
        "system_prompt",
        "guardrails",
        "memory",
        "tool_guidance",
        "subagents",
        "skills",
        "scripts",
        "plugins",
    }
    assert UPSTREAM_LICENSE == "Apache-2.0"
    assert len(UPSTREAM_REVISION) == 40


@pytest.mark.parametrize(
    "path", ["../systemprompt.md", ".env", "harnessa_policy.py", "grader/answers.json"]
)
def test_product_manifest_rejects_path_escape_and_host_or_holdout_files(path: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_product_tree({path: "must not be visible"})


def test_holdout_shaped_context_is_rejected_before_model_routing(tmp_path) -> None:
    (tmp_path / "spec.md").write_text("visible", encoding="utf-8")
    (tmp_path / "holdout").mkdir()
    (tmp_path / "holdout" / "answers.json").write_text("secret", encoding="utf-8")
    with pytest.raises(HarnessAdaptationError, match="Holdout-shaped"):
        _context_payload(tmp_path)


def test_editor_response_is_bounded_and_rejects_unknown_fields() -> None:
    assert _parse_edit_response('{"files":{"systemprompt.md":"hello"},"notes":"ok"}') == (
        {"systemprompt.md": "hello"},
        [],
        "ok",
    )
    with pytest.raises(HarnessAdaptationError):
        _parse_edit_response('{"files":{},"score":1}')


def test_context_payload_bounds_and_redacts_visible_files(tmp_path) -> None:
    (tmp_path / "instructions.md").write_text("api_key=secret-value\nvisible", encoding="utf-8")
    payload = _context_payload(tmp_path)
    assert "[REDACTED]" in payload
    assert "secret-value" not in payload
    assert _context_payload(tmp_path / "missing") == "No files are available; use only the mission text."
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "binary.bin").write_bytes(b"\xff")
    assert "binary.bin" not in _context_payload(tmp_path)


def test_chat_response_requires_text_content() -> None:
    assert _extract_chat_text({"choices": [{"message": {"content": "ok"}}]}) == "ok"
    for response in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
        with pytest.raises(HarnessAdaptationError):
            _extract_chat_text(response)


def test_candidate_edits_reject_invalid_json_and_escape_notes(tmp_path) -> None:
    workspace = tmp_path / "candidate"
    (workspace / "harness").mkdir(parents=True)
    with pytest.raises(HarnessAdaptationError):
        apply_candidate_edits(workspace, "not json")
    _, note = apply_candidate_edits(
        workspace,
        '{"files":{"systemprompt.md":"candidate"},"notes":"</notes><unsafe>"}',
    )
    assert "&lt;/notes" in note
    with pytest.raises(HarnessAdaptationError):
        apply_candidate_edits(workspace, '{"delete":["harnessa_policy.py"]}')


def test_proposer_dispatches_bounded_requests_and_validates_editor_output(tmp_path) -> None:
    from harness2.core.proposer import ProposerError

    workspace = tmp_path / "workspace"
    (workspace / "harness").mkdir(parents=True)
    frozen = FrozenExperiment(
        experiment_id=uuid4(), task_id=uuid4(), project_id=uuid4(), organization_id=uuid4(),
        base_sha="a" * 40, specification_hash="b" * 64, specification_revision=1,
        policy_revision=1, mode="parallel", k=1, route_alias="probe", image_revision="image",
        tool_revision="tools", root=tmp_path,
    )
    proposer = GatewayHarness2Proposer(frozen, max_concurrency=5)
    seen: list[dict[str, str]] = []

    async def complete(**kwargs):
        seen.append(kwargs)
        return {"choices": [{"message": {"content": '{"files":{"systemprompt.md":"edited"},"notes":"ok"}'}}]}

    proposer._complete = complete  # type: ignore[method-assign]
    reply = proposer.call("mission", workspace, "candidate", harvest="edit")
    assert reply.ok is True
    assert reply.workspace == (workspace / "harness").resolve()
    assert (workspace / "harness" / "systemprompt.md").read_text(encoding="utf-8") == "edited"
    assert seen[0]["key"].startswith(f"harness2:{frozen.experiment_id}:candidate:")
    assert "IMMUTABLE HOST POLICY" in seen[0]["system"]

    proposer.cancelled.set()
    with pytest.raises(ProposerError, match="before probe dispatch"):
        proposer.call("mission", None, "cancelled")


@pytest.mark.anyio
async def test_proposer_builds_probe_pool_request_with_stable_identity(monkeypatch) -> None:
    frozen = FrozenExperiment(
        experiment_id=uuid4(), task_id=uuid4(), project_id=uuid4(), organization_id=uuid4(),
        base_sha="a" * 40, specification_hash="b" * 64, specification_revision=1,
        policy_revision=1, mode="parallel", k=1, route_alias="probe", image_revision="image",
        tool_revision="tools", root=Path("/tmp"), proposer_run_id=uuid4(),
    )
    seen = {}

    class FakeRouter:
        async def complete(self, db, **kwargs):
            assert db is not None
            seen.update(kwargs)
            return {"response": {"choices": [{"message": {"content": "ok"}}]}}

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr("harnessa.services.harness_adaptation.SessionLocal", session)
    proposer = GatewayHarness2Proposer(frozen, model_router=FakeRouter(), max_output_tokens=128)
    response = await proposer._complete(key="stable-key", system="system", user="user", input_tokens=5)
    assert response["choices"][0]["message"]["content"] == "ok"
    request = seen["request"]
    assert request.model_alias == "probe"
    assert request.max_output_tokens == 128
    assert request.estimated_input_tokens == 5
    assert seen["idempotency_key"] == "stable-key"


def test_upstream_runner_uses_only_pinned_schedule_modes(tmp_path, monkeypatch) -> None:
    frozen = FrozenExperiment(
        experiment_id=uuid4(), task_id=uuid4(), project_id=uuid4(), organization_id=uuid4(),
        base_sha="a" * 40, specification_hash="b" * 64, specification_revision=1,
        policy_revision=1, mode="both", k=2, route_alias="probe", image_revision="image",
        tool_revision="tools", root=tmp_path,
    )
    proposer = GatewayHarness2Proposer(frozen)
    calls = []

    class ScheduleResult:
        status = "ok"

        def to_dict(self):
            return {"run": "done"}

    monkeypatch.setattr(
        "harnessa.services.harness_adaptation.harness_schedule.run_task",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or ScheduleResult()),
    )
    result = run_upstream_task(
        SimpleNamespace(domain_of=lambda _: "repo/demo", substrate=OpencodeSubstrate()),
        proposer,
        task_id="task",
        mode="both",
        k=2,
        runs_root=tmp_path / "runs",
        library_root=tmp_path / "libraries",
        task_tag="tag",
    )
    assert result["comparison_successful"] is True
    assert calls[0][1]["modes"] == ("agg", "seq")
    assert calls[0][1]["pool_policy"] == "finals"
    with pytest.raises(HarnessAdaptationError, match="recursion budget"):
        run_upstream_task(None, proposer, task_id="task", mode="parallel", k=4, runs_root=tmp_path, task_tag="bad")
    with pytest.raises(HarnessAdaptationError, match="mode"):
        run_upstream_task(None, proposer, task_id="task", mode="unknown", k=1, runs_root=tmp_path, task_tag="bad")


def test_candidate_edits_are_isolated_and_cannot_touch_host_policy(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for workspace in (first, second):
        harness = workspace / "harness" / "scripts"
        harness.mkdir(parents=True)
        (harness / "summarize.py").write_text("print('base')\n", encoding="utf-8")
    updated, _ = apply_candidate_edits(
        first,
        '{"files":{"scripts/summarize.py":"print(\\"candidate\\")\\n"},"notes":"isolated"}',
    )
    assert updated["scripts/summarize.py"] == 'print("candidate")\n'
    assert (second / "harness/scripts/summarize.py").read_text(
        encoding="utf-8"
    ) == "print('base')\n"
    with pytest.raises((HarnessAdaptationError, ValueError)):
        apply_candidate_edits(first, '{"files":{"scripts/harnessa_policy.py":"relax"}}')


def test_candidate_delete_rejects_symlink_escape(tmp_path) -> None:
    workspace = tmp_path / "candidate"
    scripts = workspace / "harness" / "scripts"
    scripts.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("host-owned", encoding="utf-8")
    (scripts / "escape.py").symlink_to(outside)
    with pytest.raises(HarnessAdaptationError, match="path escape"):
        apply_candidate_edits(workspace, '{"delete":["scripts/escape.py"]}')
    assert outside.read_text(encoding="utf-8") == "host-owned"


def test_promotion_requires_exact_candidate_and_all_fixed_gates() -> None:
    digest = "a" * 64
    result = {
        "promotion_gates": {
            "candidate_digest": digest,
            "regression": "passed",
            "security": "passed",
            "cost": "passed",
        }
    }
    assert _promotion_gates_passed(result, digest)
    assert not _promotion_gates_passed(result, "b" * 64)
    assert not _promotion_gates_passed(
        {"promotion_gates": {**result["promotion_gates"], "security": "unknown"}}, digest
    )


def test_learning_hook_requires_successful_matched_probe_evidence() -> None:
    experiment = SimpleNamespace(
        id=uuid4(),
        status="succeeded",
        base_sha="a" * 40,
        specification_hash="b" * 64,
        specification_revision=4,
        policy_revision=3,
        result={"experience_candidate": {"cue": "specific cue", "lesson": "bounded lesson"}},
    )
    probes = [
        SimpleNamespace(phase=phase, status="succeeded", trace_digest=str(index) * 64, label=phase)
        for index, phase in enumerate(("baseline", "candidate", "composed"), 1)
    ]
    identity = {
        "base_sha": experiment.base_sha,
        "specification_hash": experiment.specification_hash,
        "specification_revision": experiment.specification_revision,
        "policy_revision": experiment.policy_revision,
    }
    hook = _harness_learning_hook(experiment, probes, **identity)
    assert hook["triggered"] is True
    assert len(hook["evidence_refs"]) == 3
    assert _harness_learning_hook(experiment, probes[:2], **identity)["triggered"] is False
    assert _harness_learning_hook(
        experiment, probes, **{**identity, "specification_revision": 5}
    )["reason"] == "frozen_experiment_identity_mismatch"


def test_reviewed_rollback_restores_previous_version_and_supersedes_current() -> None:
    previous = SimpleNamespace(status="superseded")
    current = SimpleNamespace(status="active")
    apply_reviewed_experience_transition(previous, current, "rollback")
    assert previous.status == "active"
    assert current.status == "superseded"
    with pytest.raises(ValueError, match="invalid target state"):
        apply_reviewed_experience_transition(
            SimpleNamespace(status="candidate"), current, "rollback"
        )


def test_cancelled_probe_stops_before_gateway_dispatch(tmp_path: Path) -> None:
    cancelled = threading.Event()
    cancelled.set()
    proposer = GatewayHarness2Proposer(
        FrozenExperiment(
            experiment_id=uuid4(),
            task_id=uuid4(),
            project_id=uuid4(),
            organization_id=uuid4(),
            base_sha="a" * 40,
            specification_hash="b" * 64,
            specification_revision=1,
            policy_revision=1,
            mode="both",
            k=1,
            route_alias="not-used",
            image_revision="image@test",
            tool_revision="tools@test",
            root=tmp_path,
        ),
        is_cancelled=cancelled,
    )
    from harness2.core.proposer import ProposerError

    with pytest.raises(ProposerError, match="cancelled before probe dispatch"):
        proposer.call("bounded task", tmp_path, "candidate.1")


def test_pinned_upstream_demo_runs_both_modes_without_credentials(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness2.cli.demo",
            "--output",
            str(tmp_path),
            "--k",
            "1",
            "--mode",
            "both",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))
    assert report["done"] == 2 and report["failed"] == 0
    assert report["execution_count"] > 0
    assert (tmp_path / "runs" / "demo-oc-agg1" / "ledger-1" / "summary.json").is_file()
    assert (tmp_path / "executions" / "demo-oc-agg1" / "ledger-1").is_dir()
    assert (tmp_path / "executions" / "demo-oc-seq1" / "ledger-1").is_dir()
