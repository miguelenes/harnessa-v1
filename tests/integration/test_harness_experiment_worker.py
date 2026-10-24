from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from harness2.substrate.opencode import OpencodeSubstrate
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.api import harness_routes
from harnessa.contracts import TaskStage
from harnessa.db.harness import (
    EvaluationRun,
    HarnessExperience,
    HarnessExperiment,
    HarnessPolicy,
    HarnessProbeRun,
)
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import Run
from harnessa.db.review import VerificationManifestRevision  # noqa: F401
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from harnessa.domain.skill_evolution import compatibility_snapshot, validate_skill_artifact
from harnessa.services import harness_experiments as worker_module
from harnessa.services.harness_experiments import HarnessExperimentWorker
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

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
    monkeypatch.setattr(worker_module.settings, "artifact_root", str(tmp_path))
    skill_content = (
        "---\nname: active-guide\ndescription: Active skill fixture.\nlicense: MIT\n"
        "metadata:\n  dependencies: []\nallowed-tools: []\n---\n\nRead only guidance.\n"
    )
    skill = validate_skill_artifact("skills/active-guide/SKILL.md", skill_content)
    stored_skill = LocalArtifactStore(tmp_path).put_bytes(
        skill_content.encode(), media_type="text/markdown"
    )
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
        experience = HarnessExperience(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            source_task_id=task.id, experience_key="active-guide", version=1, status="active",
            repository_scope=repository.target_id, domain_scope="repository",
            cue="active-guide", lesson="Use read-only guidance", evidence_refs=[{"sha256": "f" * 64}],
            candidate_digest="e" * 64, conflict_key="active-guide", source_kind="operator_authored",
            author="fixture operator", producer="integration test", scope_type="repository",
            scope_id=repository.target_id, license_id="MIT", artifact_path=skill.path,
            artifact_digest=stored_skill.digest, artifact_storage_key=stored_skill.storage_key,
            artifact_size_bytes=stored_skill.size_bytes,
            confidence_rationale="A deterministic integration fixture.",
            compatibility_snapshot=compatibility_snapshot(
                harness2_revision=experiment.harness2_revision,
                tool_revision=worker_module.settings.acp_harness_revision,
                policy_revision=policy.revision, model_capability_revision=None,
                route_alias="probe-route", executor_image=worker_module.settings.acp_agent_image,
                repository=repository.target_id, task=None, organization=None,
            ),
            expiry_at=datetime.now(UTC) + timedelta(days=1), policy_revision=policy.revision,
        )
        selected_experience = HarnessExperience(
            id=uuid4(), organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            source_task_id=task.id, experience_key="selected-guide", version=2, status="candidate",
            repository_scope=repository.target_id, domain_scope="repository",
            cue="selected-guide", lesson="Use the selected frozen guidance",
            evidence_refs=[{"sha256": "d" * 64}], candidate_digest="c" * 64,
            conflict_key="selected-guide", source_kind="operator_authored",
            author="fixture operator", producer="integration test", scope_type="repository",
            scope_id=repository.target_id, license_id="MIT", artifact_path=skill.path,
            artifact_digest=stored_skill.digest, artifact_storage_key=stored_skill.storage_key,
            artifact_size_bytes=stored_skill.size_bytes,
            confidence_rationale="A deterministic selected-skill fixture.",
            compatibility_snapshot={}, expiry_at=datetime.now(UTC) + timedelta(days=1),
            policy_revision=policy.revision,
        )
        experiment.result = {
            "route_alias": "probe-route",
            "skill_versions": [{
                "id": str(selected_experience.id), "version": selected_experience.version,
                "candidate_digest": selected_experience.candidate_digest,
                "artifact_digest": selected_experience.artifact_digest,
                "artifact_storage_key": selected_experience.artifact_storage_key,
                "artifact_path": selected_experience.artifact_path,
                "artifact_size_bytes": selected_experience.artifact_size_bytes,
            }],
        }
        db.add_all([interview, experiment, policy, experience, selected_experience])
        await db.commit()
        experiment_id = experiment.id

    class SpecificationManager:
        async def read_file_at_revision(self, *_: object, **__: object) -> bytes:
            return b"# Visible base harness\n"

    monkeypatch.setattr(worker_module, "specification_workspace_manager", lambda: SpecificationManager())
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
    assert frozen.skill_artifacts[0]["path"] == skill.path
    assert frozen.skill_artifacts[0]["digest"] == skill.digest
    assert [item.id for item in experiences] == [experience.id]
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
async def test_skill_import_scopes_are_validated_and_organization_skills_are_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await gateway_fixture()
    monkeypatch.setattr(harness_routes.settings, "artifact_root", str(tmp_path))
    content = (
        "---\nname: scoped-skill\ndescription: A scoped integration fixture.\n"
        "license: MIT\nmetadata:\n  dependencies: []\nallowed-tools: []\n---\n\n"
        "Use this only as bounded guidance.\n"
    )
    async with SessionLocal() as db:
        actor = await db.get(harness_routes.User, fixture["user_id"])
        assert actor is not None
        project = await db.get(Project, fixture["project_id"])
        assert project is not None
        task = await db.get(Task, fixture["task_id"])
        assert task is not None
        task_candidate = await harness_routes.create_skill_candidate(
            project.id,
            harness_routes.SkillImportRequest(
                source_kind="operator_authored",
                scope_type="task",
                scope_id=str(task.id),
                skill_path="skills/scoped-skill/SKILL.md",
                content=content,
            ),
            actor,
            db,
        )
        task_row = await db.get(HarnessExperience, UUID(task_candidate["id"]))
        assert task_row is not None
        assert (task_row.scope_type, task_row.scope_id) == ("task", str(task.id))
        assert task_row.status == "candidate"
        task_replay = await harness_routes.create_skill_candidate(
            project.id,
            harness_routes.SkillImportRequest(
                source_kind="operator_authored", scope_type="task", scope_id=str(task.id),
                skill_path="skills/scoped-skill/SKILL.md", content=content,
            ),
            actor,
            db,
        )
        assert task_replay["id"] == task_candidate["id"]
        assert task_replay["idempotent_replay"] is True
        artifact_response = await harness_routes.read_skill_artifact(
            project.id, task_row.id, actor, db
        )
        assert artifact_response.body.decode("utf-8") == content
        conflicting_content = content.replace(
            "bounded guidance", "different bounded guidance"
        )
        conflict_candidate = await harness_routes.create_skill_candidate(
            project.id,
            harness_routes.SkillImportRequest(
                source_kind="operator_authored", scope_type="task", scope_id=str(task.id),
                skill_path="skills/scoped-skill/SKILL.md", content=conflicting_content,
            ),
            actor,
            db,
        )
        assert conflict_candidate["conflict_state"] == "conflict"

        organization_content = content.replace("scoped-skill", "shared-skill")
        organization_candidate = await harness_routes.create_skill_candidate(
            project.id,
            harness_routes.SkillImportRequest(
                source_kind="operator_authored",
                scope_type="organization",
                scope_id=str(project.organization_id),
                skill_path="skills/shared-skill/SKILL.md",
                content=organization_content,
            ),
            actor,
            db,
        )
        organization_row = await db.get(HarnessExperience, UUID(organization_candidate["id"]))
        assert organization_row is not None
        assert (organization_row.scope_type, organization_row.scope_id) == (
            "organization",
            str(project.organization_id),
        )
        task_promotion = harness_routes.PromotionRequest(
            stable_key="task-skill-cannot-promote", candidate_digest=task_candidate["candidate_digest"],
            rationale="Task scope remains local.",
        )
        with pytest.raises(HTTPException) as task_blocked:
            await harness_routes.promote_harness_experience(
                project.id, task_row.id, task_promotion, actor, db
            )
        assert task_blocked.value.status_code == 409
        assert "Task-scoped skills cannot be activated" in task_blocked.value.detail
        organization_promotion = harness_routes.PromotionRequest(
            stable_key="org-skill-needs-eval", candidate_digest=organization_candidate["candidate_digest"],
            rationale="Organization promotion needs a matching evaluation.",
        )
        with pytest.raises(HTTPException) as org_blocked:
            await harness_routes.promote_harness_experience(
                project.id, organization_row.id, organization_promotion, actor, db
            )
        assert org_blocked.value.status_code == 409
        assert "matching, passing multi-task evaluation" in org_blocked.value.detail
        other_project = Project(
            organization_id=project.organization_id,
            name="Shared skill project",
            slug=f"shared-{uuid4().hex[:10]}",
        )
        db.add(other_project)
        await db.commit()
        overview = await harness_routes.harness_overview(other_project.id, actor, db)

        assert any(
            item["id"] == organization_candidate["id"]
            for item in overview.experiences
        )


@pytest.mark.anyio
async def test_postgres_skill_trigger_blocks_direct_content_mutation_but_allows_stale_status() -> None:
    fixture = await gateway_fixture()
    async with SessionLocal() as db:
        row = HarnessExperience(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            source_task_id=None,
            source_experiment_id=None,
            source_run_id=None,
            source_evaluation_run_id=None,
            experience_key="immutable-skill-trigger",
            version=1,
            status="candidate",
            repository_scope="repo/immutable",
            domain_scope="skill",
            cue="immutable-skill",
            lesson="Original immutable content.",
            evidence_refs=[],
            candidate_digest="a" * 64,
            conflict_key="immutable-skill",
            source_kind="operator_authored",
            author="test operator",
            producer="harnessa-integration-test",
            scope_type="repository",
            scope_id="repo/immutable",
            license_id="MIT",
            dependencies=[],
            allowed_tools=[],
            trust_level="untrusted",
            artifact_path="skills/immutable-skill/SKILL.md",
            artifact_digest="b" * 64,
            artifact_storage_key="sha256/bb/" + "b" * 64,
            artifact_size_bytes=10,
            confidence_rationale="An integration check for immutable provenance.",
            compatibility_snapshot={},
            expiry_at=datetime.now(UTC) + timedelta(days=1),
            policy_revision=1,
        )
        db.add(row)
        await db.commit()
        row_id = row.id
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(HarnessExperience)
                .where(HarnessExperience.id == row_id)
                .values(lesson="Tampered content.")
            )
        await db.rollback()
        row = await db.get(HarnessExperience, row_id)
        assert row is not None and row.lesson == "Original immutable content."
        row.status = "stale"
        row.stale_reason = {"compatibility": {"recorded": "old", "current": "new"}}
        await db.commit()


@pytest.mark.anyio
async def test_skill_promotion_gate_requires_a_matched_multi_task_holdout_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await gateway_fixture()
    monkeypatch.setattr(harness_routes.settings, "artifact_root", str(tmp_path))
    content = (
        "---\nname: evaluated-skill\ndescription: Evaluated fixture.\nlicense: MIT\n"
        "metadata:\n  dependencies: []\nallowed-tools: []\n---\n\nUse matched evidence.\n"
    )
    artifact = validate_skill_artifact("skills/evaluated-skill/SKILL.md", content)
    stored = LocalArtifactStore(tmp_path).put_bytes(content.encode(), media_type="text/markdown")
    async with SessionLocal() as db:
        second_task = Task(
            project_id=fixture["project_id"], title="Second heldout task",
            description="Bounded fixture", stage=TaskStage.HARNESS_PROBE.value,
        )
        candidate = HarnessExperience(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            source_task_id=fixture["task_id"], experience_key="evaluated-skill", version=1,
            status="candidate", repository_scope="*", domain_scope="project",
            cue="evaluated-skill", lesson="Use matched evidence", evidence_refs=[{"digest": "a" * 64}],
            candidate_digest="b" * 64, conflict_key="evaluated-skill", source_kind="operator_authored",
            author="fixture operator", producer="integration test", scope_type="project",
            scope_id=str(fixture["project_id"]), license_id="MIT", artifact_path=artifact.path,
            artifact_digest=stored.digest, artifact_storage_key=stored.storage_key,
            artifact_size_bytes=stored.size_bytes, confidence_rationale="Bounded fixture evidence.",
            compatibility_snapshot={}, expiry_at=datetime.now(UTC) + timedelta(days=1), policy_revision=1,
        )
        db.add_all([second_task, candidate])
        await db.flush()
        task_ids = [str(fixture["task_id"]), str(second_task.id)]
        evaluation = EvaluationRun(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            created_by=fixture["user_id"], stable_key="skill-gate-evaluation", status="succeeded",
            manifest={
                "skill_versions": [{"id": str(candidate.id), "version": candidate.version,
                                    "candidate_digest": candidate.candidate_digest,
                                    "artifact_digest": candidate.artifact_digest}],
                "task_inputs": [{"task_id": task_id} for task_id in task_ids],
            }, manifest_digest="c" * 64, experiment_ids=[], max_cost_usd="1.00000000",
        )
        db.add(evaluation)
        await db.flush()
        children: list[HarnessExperiment] = []
        for index, task_id in enumerate([fixture["task_id"], second_task.id]):
            child = HarnessExperiment(
                organization_id=fixture["organization_id"], project_id=fixture["project_id"],
                task_id=task_id, evaluation_run_id=evaluation.id, created_by=fixture["user_id"],
                stable_key=f"skill-gate-child-{index}", mode="parallel", k=1,
                status="succeeded", phase="complete", attempts=1, base_sha="d" * 40,
                specification_hash="e" * 64, specification_revision=1, policy_revision=1,
                harness2_revision="f" * 40, result_digest="1" * 64,
                result={
                    "comparison_successful": True, "composed_successful": True,
                    "holdout_manifest": {
                        "digest": "2" * 64,
                        "skill_versions": [{"experience_id": str(candidate.id),
                                            "version": candidate.version,
                                            "digest": candidate.artifact_digest}],
                    },
                    "holdout_evaluation": {
                        "status": "passed", "manifest_digest": "2" * 64,
                        "result_digest": "3" * 64,
                    },
                },
            )
            db.add(child)
            children.append(child)
        await db.flush()
        evaluation.experiment_ids = [str(item.id) for item in children]
        db.add_all([
            HarnessProbeRun(
                organization_id=fixture["organization_id"], project_id=fixture["project_id"],
                task_id=child.task_id, experiment_id=child.id, label=f"gate-probe-{index}",
                phase="baseline", status="succeeded", delivered=True, wall_ms=100,
                cost_usd="0.0001",
            )
            for index, child in enumerate(children)
        ])
        await db.flush()
        gate = await harness_routes._skill_evaluation_gate(db, fixture["project_id"], candidate)

    assert gate is not None
    assert gate["sample_size"] == 2
    assert gate["missing_tasks"] == gate["failed_tasks"] == gate["unknown_cost_or_outcome"] == 0
    assert gate["candidate_digest"] == "b" * 64


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
