from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harness2.substrate.opencode import OpencodeSubstrate
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.api import harness_routes
from harnessa.contracts import TaskStage
from harnessa.db.harness import HarnessExperience, HarnessExperiment, HarnessPolicy, HarnessProbeRun
from harnessa.db.models import Repository, Task
from harnessa.db.orchestration import Run
from harnessa.db.review import VerificationManifestRevision  # noqa: F401
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.services import harness_experiments as worker_module
from harnessa.services.harness_experiments import HarnessExperimentWorker
from sqlalchemy import select

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_worker_recovers_stale_claim_and_finalizes_only_its_current_fence() -> None:
    fixture = await gateway_fixture()
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        task = await db.get(Task, fixture["task_id"])
        assert task is not None
        task.stage = "HARNESS_PROBE"
        experiment = HarnessExperiment(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=task.id,
            created_by=fixture["user_id"],
            stable_key="stale-recovery-fixture",
            mode="parallel",
            k=1,
            status="running",
            phase="baseline_candidate_composed",
            attempts=1,
            base_sha="a" * 40,
            specification_hash="b" * 64,
            specification_revision=1,
            policy_revision=1,
            harness2_revision="c" * 40,
            started_at=now - timedelta(minutes=5),
            worker_heartbeat_at=now - timedelta(minutes=5),
        )
        db.add(experiment)
        await db.flush()
        rollout = Run(
            task_id=task.id,
            project_id=fixture["project_id"],
            attempt=2,
            status="RUNNING",
            fencing_token=1,
            started_at=now - timedelta(minutes=5),
            adapter_name="harness2-probe",
            execution_result={"experiment_id": str(experiment.id)},
        )
        policy = HarnessPolicy(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            revision=1,
        )
        db.add_all([rollout, policy])
        await db.commit()
        experiment_id, task_id, run_id = experiment.id, task.id, rollout.id

    worker = HarnessExperimentWorker(None)
    assert await worker._claim() == (experiment_id, 2)
    assert await worker.is_current_fence(run_id, 2) is False
    assert await worker._cancelled(experiment_id, 2) is True
    await worker._heartbeat(experiment_id, 2)

    async with SessionLocal() as db:
        refreshed = await db.get(HarnessExperiment, experiment_id)
        stale_run = await db.get(Run, run_id)
        assert refreshed is not None and stale_run is not None
        assert refreshed.attempts == 2
        assert refreshed.status == "running"
        assert refreshed.worker_heartbeat_at is not None
        assert stale_run.status == "FAILED"
        assert stale_run.error_code == "experiment_lease_expired"
        db.add(Run(
            task_id=task_id,
            project_id=refreshed.project_id,
            attempt=3,
            status="RUNNING",
            fencing_token=2,
            started_at=now,
            adapter_name="harness2-probe",
            execution_result={"experiment_id": str(experiment_id)},
        ))
        await db.commit()

    async with SessionLocal() as db:
        current_run = await db.scalar(select(Run).where(
            Run.task_id == task_id,
            Run.fencing_token == 2,
            Run.adapter_name == "harness2-probe",
        ))
        assert current_run is not None
        assert await worker.is_current_fence(current_run.id, 2) is True
    await worker._finish(experiment_id, 2, "failed", "fixture failure")

    async with SessionLocal() as db:
        final = await db.get(HarnessExperiment, experiment_id)
        assert final is not None
        assert final.status == "failed"
        assert final.last_error == "fixture failure"
        runs = list(await db.scalars(select(Run).where(
            Run.task_id == task_id,
            Run.adapter_name.in_(("harness2-probe", "harness2-proposer")),
        )))
        assert all(run.status != "RUNNING" for run in runs)


@pytest.mark.anyio
async def test_inputs_freeze_ready_spec_and_persist_matched_probe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await gateway_fixture()
    base_sha = "a" * 40
    spec_hash = "b" * 64
    async with SessionLocal() as db:
        task = await db.get(Task, fixture["task_id"])
        assert task is not None
        task.stage = "HARNESS_PROBE"
        repository = Repository(
            project_id=fixture["project_id"],
            target_id="repo/harness-fixture",
            display_name="Harness fixture",
        )
        db.add(repository)
        await db.flush()
        interview = SpecificationInterview(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=task.id,
            repository_id=repository.id,
            created_by=fixture["user_id"],
            topic="Harness experiment fixture",
            change_id="harness-experiment-fixture",
            base_sha=base_sha,
            head_sha=base_sha,
            idempotency_key="harness-input-fixture",
            request_hash="c" * 64,
            artifact_hash=spec_hash,
            readiness_state="READY",
            revision=3,
        )
        experiment = HarnessExperiment(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=task.id,
            created_by=fixture["user_id"],
            stable_key="ready-input-fixture",
            mode="parallel",
            k=1,
            status="running",
            phase="baseline_candidate_composed",
            attempts=1,
            base_sha=base_sha,
            specification_hash=spec_hash,
            specification_revision=3,
            policy_revision=2,
            harness2_revision="d" * 40,
            result={"route_alias": "probe-route"},
        )
        policy = HarnessPolicy(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            revision=2,
            max_probe_concurrency=2,
        )
        db.add_all([interview, experiment, policy])
        await db.commit()
        experiment_id = experiment.id

    class SpecificationManager:
        async def read_file_at_revision(self, *_: object, **__: object) -> bytes:
            return b"# Visible base harness\n"

    monkeypatch.setattr(worker_module, "specification_workspace_manager", lambda: SpecificationManager())
    monkeypatch.setattr(worker_module.settings, "artifact_root", str(tmp_path))
    class Evaluator:
        async def evaluate(self, candidate: dict[str, object]) -> dict[str, str]:
            artifacts = candidate["artifacts"]
            assert isinstance(artifacts, list) and artifacts
            assert artifacts[0]["trace"]["sha256"]
            assert artifacts[0]["artifacts"][0]["media_type"] == "text/plain"
            candidate_artifact = candidate["candidate"]
            assert isinstance(candidate_artifact, dict)
            assert candidate_artifact["sha256"]
            return {
                "status": "passed",
                "manifest_digest": str(candidate["digest"]),
                "judge_revision": "offline-holdout-fixture-v1",
                "private_feedback": "fixture answer must never enter experiment result",
            }

    worker = HarnessExperimentWorker(object(), evaluator=Evaluator())  # type: ignore[arg-type]
    frozen, task_input, organization_id, scope, base_harness, experiences, concurrency = await worker._inputs(
        experiment_id, 1
    )
    assert frozen.base_sha == base_sha
    assert frozen.specification_hash == spec_hash
    assert task_input.approved_base_sha == base_sha
    assert organization_id == fixture["organization_id"]
    assert scope == "repo/harness-fixture"
    assert (base_harness / "systemprompt.md").read_text(encoding="utf-8") == "# Visible base harness\n"
    assert experiences == []
    assert concurrency == 2

    class FakeExecution:
        async def execute(self, task_value, run_value, **callbacks):
            assert await callbacks["is_current_fence"](run_value.id, run_value.fencing_token)
            await callbacks["save_checkpoint"]({"checkpoint": "safe"})
            artifact = await callbacks["publish_artifact"](b"patch fixture", "text/plain")
            return {
                "status": "completed",
                "source_revision": base_sha,
                "patch_artifact": artifact,
                "changed_files": [],
            }

    execution_adapter = FakeExecution()
    task_adapter = worker_module.HarnessACPTaskAdapter(
        frozen=frozen,
        execution=execution_adapter,
        task=task_input,
        organization_id=organization_id,
        repository_scope=scope,
        base_harness=base_harness,
        max_concurrency=concurrency,
        event_loop=asyncio.get_running_loop(),
    )
    rollout_id = uuid4()
    run_result = await task_adapter._execute(rollout_id, base_harness)
    assert run_result["status"] == "completed"
    async with SessionLocal() as db:
        rollout = await db.get(Run, rollout_id)
        assert rollout is not None and rollout.status == "SUCCEEDED"

    trace_root = tmp_path / "trace"
    trace_root.mkdir()
    (trace_root / "trajectory.txt").write_text('{"status":"completed"}', encoding="utf-8")
    probe_run_id = rollout_id
    tree = {"systemprompt.md": "# Visible base harness\n"}
    adapter = type(
        "EvidenceAdapter",
        (),
        {
            "substrate": OpencodeSubstrate(),
            "records": [{
                "run_uuid": str(probe_run_id),
                "run_dir": str(trace_root),
                "label": "baseline-fixture",
                "phase": "baseline",
                "status": "succeeded",
                "delivered": True,
                "wall_ms": 12,
                "harness_digest": "e" * 64,
                "tree": tree,
                "outcome": '{"status":"completed"}',
            }],
        },
    )()
    adapter.base_harness = lambda: base_harness
    candidate_bytes = b'{"schema_version":1,"components":{"systemprompt.md":"review me"}}'
    candidate_stored = LocalArtifactStore(tmp_path).put_bytes(
        candidate_bytes,
        media_type="application/vnd.harnessa.harness-candidate+json",
    )
    candidate_ref = {
        "sha256": candidate_stored.digest,
        "storage_key": candidate_stored.storage_key,
        "size_bytes": candidate_stored.size_bytes,
        "media_type": candidate_stored.media_type,
    }
    result = {
        "comparison_successful": True,
        "composed_successful": True,
        "cost_gate": {"status": "passed"},
        "experience_candidate": {
            "cue": "fixture cue",
            "lesson": "fixture lesson",
            "repository_scope": scope,
            "domain_scope": "repository",
            "component_digest": candidate_stored.digest,
            "component_artifact": candidate_ref,
            "changed_components": ["systemprompt.md"],
        },
    }
    await worker._persist(frozen, adapter, result)  # type: ignore[arg-type]
    holdout = await worker._grade_holdout(frozen, result)
    assert holdout["status"] == "passed"
    assert "private_feedback" not in holdout
    assert result["promotion_gates"]["candidate_digest"] == (
        result["experience_candidate"]["candidate_digest"]
    )
    assert result["promotion_gates"]["regression"] == "passed"
    assert result["promotion_gates"]["security"] == "passed"
    assert result["promotion_gates"]["cost"] == "blocked"
    assert not harness_routes._promotion_gates_passed(
        result, result["experience_candidate"]["candidate_digest"]
    )

    async with SessionLocal() as db:
        probe = await db.scalar(select(HarnessProbeRun).where(
            HarnessProbeRun.experiment_id == experiment_id
        ))
        persisted = await db.get(HarnessExperiment, experiment_id)
        assert probe is not None and persisted is not None
        assert probe.phase == "baseline"
        assert probe.trace_digest
        assert persisted.result["comparison_successful"] is True
        assert persisted.result_digest
        assert persisted.result["holdout_evaluation"]["judge_revision"] == "offline-holdout-fixture-v1"
        assert "private_feedback" not in persisted.result["holdout_evaluation"]
        assert persisted.result["experience_candidate"]["evidence_refs"]
        assert persisted.result["holdout_manifest"]["candidate_digest"] == (
            persisted.result["experience_candidate"]["candidate_digest"]
        )
        candidate_metadata_ref = persisted.result["holdout_manifest"]["candidate_metadata"]
        assert candidate_metadata_ref == persisted.result["experience_candidate"]["manifest_artifact"]
        assert LocalArtifactStore(tmp_path).verify_reference(
            candidate_metadata_ref["storage_key"],
            expected_digest=candidate_metadata_ref["sha256"],
            expected_size=candidate_metadata_ref["size_bytes"],
        ) == "verified"
        candidate_metadata = json.loads(
            (tmp_path / candidate_metadata_ref["storage_key"]).read_bytes()
        )
        assert candidate_metadata["candidate_digest"] == (
            persisted.result["experience_candidate"]["candidate_digest"]
        )
        assert candidate_metadata["evidence_refs"]
        assert persisted.result["holdout_manifest"]["artifacts"][0]["artifacts"][0]["sha256"]
        assert "storage_key" not in persisted.result["holdout_manifest"]["artifacts"][0]["artifacts"][0]


@pytest.mark.anyio
async def test_experiment_route_freezes_inputs_and_enforces_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await gateway_fixture()

    async def operator_membership(*_: object, **__: object) -> object:
        return SimpleNamespace(role="operator")

    monkeypatch.setattr(harness_routes, "require_membership", operator_membership)
    async with SessionLocal() as db:
        task = await db.get(Task, fixture["task_id"])
        assert task is not None
        task.stage = TaskStage.HARNESS_PROBE.value
        repository = Repository(
            project_id=fixture["project_id"],
            target_id="repo/route-fixture",
            display_name="Route fixture",
        )
        db.add(repository)
        await db.flush()
        db.add(SpecificationInterview(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=task.id,
            repository_id=repository.id,
            created_by=fixture["user_id"],
            topic="Frozen harness input",
            change_id="harness-route-fixture",
            base_sha="a" * 40,
            head_sha="a" * 40,
            idempotency_key="harness-route-idempotency",
            request_hash="c" * 64,
            artifact_hash="b" * 64,
            readiness_state="READY",
            revision=7,
        ))
        await db.commit()
        actor = await db.get(harness_routes.User, fixture["user_id"])
        assert actor is not None
        body = harness_routes.ExperimentCreate(
            stable_key="route-idempotency-key",
            mode="both",
            k=2,
            route_alias="probe-route",
        )

        created = await harness_routes.create_harness_experiment(
            fixture["project_id"], task.id, body, actor, db
        )
        repeated = await harness_routes.create_harness_experiment(
            fixture["project_id"], task.id, body, actor, db
        )
        assert created.id == repeated.id
        assert created.status == "queued"
        assert created.base_sha == "a" * 40
        assert created.specification_revision == 7
        assert created.result["route_alias"] == "probe-route"

        row = await db.get(HarnessExperiment, created.id)
        assert row is not None
        row.status = "failed"
        row.last_error = "provider unavailable"
        await db.commit()
        resumed = await harness_routes.resume_harness_experiment(
            fixture["project_id"], created.id, actor, db
        )
        assert resumed.status == "queued"
        assert resumed.result["resume_count"] == 1
        assert resumed.base_sha == created.base_sha

        with pytest.raises(HTTPException) as conflict:
            await harness_routes.create_harness_experiment(
                fixture["project_id"], task.id,
                body.model_copy(update={"mode": "parallel"}), actor, db,
            )
        assert conflict.value.status_code == 409

        task.stage = "IN_PROGRESS"
        with pytest.raises(HTTPException) as wrong_stage:
            await harness_routes.create_harness_experiment(
                fixture["project_id"], task.id,
                body.model_copy(update={"stable_key": "wrong-stage"}), actor, db,
            )
        assert wrong_stage.value.status_code == 409


@pytest.mark.anyio
async def test_harness_overview_returns_policy_experiments_probes_and_experiences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await gateway_fixture()

    async def member(*_: object, **__: object) -> object:
        return SimpleNamespace(role="viewer")

    monkeypatch.setattr(harness_routes, "require_membership", member)
    async with SessionLocal() as db:
        experiment = HarnessExperiment(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=fixture["task_id"],
            created_by=fixture["user_id"],
            stable_key="overview-fixture",
            mode="parallel",
            k=1,
            status="succeeded",
            phase="complete",
            attempts=1,
            base_sha="a" * 40,
            specification_hash="b" * 64,
            specification_revision=2,
            policy_revision=3,
            harness2_revision="c" * 40,
            result={"route_alias": "probe-route"},
        )
        db.add_all([
            HarnessPolicy(
                organization_id=fixture["organization_id"],
                project_id=fixture["project_id"],
                revision=3,
            ),
            experiment,
        ])
        await db.flush()
        db.add_all([
            HarnessProbeRun(
                organization_id=fixture["organization_id"],
                project_id=fixture["project_id"],
                task_id=fixture["task_id"],
                experiment_id=experiment.id,
                label="baseline",
                phase="baseline",
                status="succeeded",
                delivered=True,
                wall_ms=10,
                route_alias="probe-route",
                harness_digest="d" * 64,
                trace_digest="e" * 64,
                component_diff={"changed": []},
                outcome_detail="completed",
            ),
            HarnessExperience(
                organization_id=fixture["organization_id"],
                project_id=fixture["project_id"],
                source_task_id=fixture["task_id"],
                source_experiment_id=experiment.id,
                experience_key="overview-experience",
                version=1,
                status="candidate",
                repository_scope="repo/demo",
                domain_scope="coding",
                cue="When X happens",
                lesson="Apply bounded retry guidance",
                evidence_refs=[{"phase": "baseline"}],
                candidate_digest="f" * 64,
                conflict_key="retry-guidance",
                expiry_at=datetime.now(UTC) + timedelta(days=1),
                policy_revision=3,
            ),
        ])
        await db.commit()

        overview = await harness_routes.harness_overview(fixture["project_id"], object(), db)

    assert overview.policy["revision"] == 3
    assert overview.experiments[0]["upstream_revision"] == "c" * 40
    assert overview.probes[0]["trace_digest"] == "e" * 64
    assert overview.experiences[0]["revalidated_at"] is None
    assert overview.manifest["immutable_policy"]["revision"] == "fixed-v1"


@pytest.mark.anyio
async def test_reviewed_experience_promotion_and_rollback_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await gateway_fixture()
    digest, old_digest = "a" * 64, "b" * 64

    async def administrator(*_: object, **__: object) -> object:
        return SimpleNamespace(role="administrator")

    monkeypatch.setattr(harness_routes, "require_membership", administrator)
    async with SessionLocal() as db:
        actor = await db.get(harness_routes.User, fixture["user_id"])
        assert actor is not None
        policy = HarnessPolicy(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"], revision=4,
        )
        experiment = HarnessExperiment(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            task_id=fixture["task_id"], created_by=fixture["user_id"], stable_key="promotion-source",
            mode="parallel", k=1, status="succeeded", phase="complete", attempts=1,
            base_sha="c" * 40, specification_hash="d" * 64, specification_revision=1,
            policy_revision=4, harness2_revision="e" * 40,
            result={
                "promotion_gates": {
                    "candidate_digest": digest, "regression": "passed", "security": "passed", "cost": "passed",
                },
                "holdout_manifest": {"digest": "c" * 64},
                "holdout_evaluation": {
                    "status": "passed", "manifest_digest": "c" * 64, "judge_revision": "fixture-v1",
                },
            },
        )
        db.add_all([policy, experiment])
        await db.flush()
        candidate = HarnessExperience(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            source_task_id=fixture["task_id"], source_experiment_id=experiment.id,
            experience_key="retry-lesson", version=2, status="candidate", repository_scope="repo/demo",
            domain_scope="coding", cue="On transient failure", lesson="Use bounded retry",
            evidence_refs=[{"phase": "baseline"}], candidate_digest=digest, conflict_key="retry",
            expiry_at=datetime.now(UTC) + timedelta(days=1), policy_revision=4,
        )
        db.add(candidate)
        await db.flush()
        previous = HarnessExperience(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            source_task_id=fixture["task_id"], source_experiment_id=experiment.id,
            experience_key="retry-lesson", version=1, status="superseded", repository_scope="repo/demo",
            domain_scope="coding", cue="On transient failure", lesson="Prior retry guidance",
            evidence_refs=[{"phase": "baseline"}], candidate_digest=old_digest, conflict_key="retry",
            expiry_at=datetime.now(UTC) + timedelta(days=1), policy_revision=4,
        )
        db.add(previous)
        await db.commit()

        promote_body = harness_routes.PromotionRequest(
            stable_key="promote-retry-lesson", candidate_digest=digest, rationale="Exact fixed gates passed.",
        )
        promoted = await harness_routes.promote_harness_experience(
            fixture["project_id"], candidate.id, promote_body, actor, db,
        )
        repeated = await harness_routes.promote_harness_experience(
            fixture["project_id"], candidate.id, promote_body, actor, db,
        )
        assert promoted["decision"] == repeated["decision"] == "approved"
        assert promoted["previous_experience_id"] is None

        rollback_body = harness_routes.PromotionRequest(
            stable_key="rollback-retry-lesson", candidate_digest=old_digest, rationale="Restore prior version.",
        )
        rolled_back = await harness_routes.rollback_harness_experience(
            fixture["project_id"], previous.id, rollback_body, actor, db,
        )
        repeated_rollback = await harness_routes.rollback_harness_experience(
            fixture["project_id"], previous.id, rollback_body, actor, db,
        )
        assert rolled_back["decision"] == repeated_rollback["decision"] == "approved"
        assert rolled_back["rolled_back_from"] == str(candidate.id)
        refreshed = await db.get(HarnessExperience, previous.id)
        assert refreshed is not None and refreshed.status == "active"
