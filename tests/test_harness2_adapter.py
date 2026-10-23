from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
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
    _parse_edit_response,
    apply_candidate_edits,
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
        result={"experience_candidate": {"cue": "specific cue", "lesson": "bounded lesson"}},
    )
    probes = [
        SimpleNamespace(phase=phase, status="succeeded", trace_digest=str(index) * 64, label=phase)
        for index, phase in enumerate(("baseline", "candidate", "composed"), 1)
    ]
    hook = _harness_learning_hook(experiment, probes)
    assert hook["triggered"] is True
    assert len(hook["evidence_refs"]) == 3
    assert _harness_learning_hook(experiment, probes[:2])["triggered"] is False


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
