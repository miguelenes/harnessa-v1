from __future__ import annotations

import pytest
from fastapi import HTTPException
from harnessa.api import evaluation_routes, execution_routes
from harnessa.contracts import ModelPoolKind
from harnessa.db.harness import EvaluationRun, HarnessExperiment, HarnessPolicy, HarnessProbeRun
from harnessa.db.models import Repository, Task
from harnessa.db.orchestration import Run
from harnessa.db.review import VerificationManifestRevision  # noqa: F401
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview
from sqlalchemy import select

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_project_runner_selection_is_persisted_and_rejects_unavailable_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(execution_routes.User, fixture["user_id"])
        assert actor is not None
        project = await db.get(execution_routes.Project, fixture["project_id"])
        assert project is not None
        assert (await execution_routes.get_execution_adapter(project.id, actor, db)).adapter == "deployment-default"
        disabled = await execution_routes.set_execution_adapter(
            project.id, execution_routes.ExecutionAdapterWrite(adapter="disabled"), actor, db
        )
        assert disabled.adapter == "disabled"
        with pytest.raises(HTTPException) as unavailable:
            await execution_routes.set_execution_adapter(
                project.id, execution_routes.ExecutionAdapterWrite(adapter="acp-opencode"), actor, db
            )
        assert unavailable.value.status_code == 409
        monkeypatch.setattr(execution_routes.settings, "acp_execution_enabled", True)
        configured = await execution_routes.set_execution_adapter(
            project.id, execution_routes.ExecutionAdapterWrite(adapter="acp-opencode"), actor, db
        )
        assert configured.adapter == "acp-opencode"
        assert configured.configured is True


@pytest.mark.anyio
async def test_grouped_evaluation_freezes_inputs_estimates_and_cancels_children() -> None:
    fixture = await gateway_fixture()
    async with SessionLocal() as db:
        budget = await db.scalar(select(evaluation_routes.ModelBudgetPolicy).where(
            evaluation_routes.ModelBudgetPolicy.project_id == fixture["project_id"]
        ))
        assert budget is not None
        budget.max_steps = 1
        await db.flush()
        task = await db.get(Task, fixture["task_id"])
        assert task is not None
        task.stage = "HARNESS_PROBE"
        repository = Repository(
            project_id=fixture["project_id"], target_id="repo/evaluation-fixture",
            display_name="Evaluation fixture",
        )
        db.add(repository)
        await db.flush()
        db.add(SpecificationInterview(
            organization_id=fixture["organization_id"], project_id=fixture["project_id"],
            task_id=task.id, repository_id=repository.id, created_by=fixture["user_id"],
            topic="Frozen evaluation input", change_id="evaluation-run-fixture",
            base_sha="a" * 40, head_sha="a" * 40, idempotency_key="evaluation-idem",
            request_hash="c" * 64, artifact_hash="b" * 64, readiness_state="READY", revision=2,
        ))
        actor = await db.get(evaluation_routes.User, fixture["user_id"])
        assert actor is not None
        body = evaluation_routes.EvaluationRunCreate(
            stable_key="grouped-evaluation-fixture", suite="harness2-local",
            task_ids=[task.id], mode="both", k=1, route_alias="coding",
        )
        estimate = await evaluation_routes.evaluation_dry_run(
            fixture["project_id"], body, actor, db
        )
        assert estimate.estimate_kind == "conservative_context_window_upper_bound"
        assert estimate.task_count == 1 and estimate.within_project_policy is True
        assert estimate.cost_estimate_usd == "3.10500000"
        created = await evaluation_routes.create_evaluation_run(
            fixture["project_id"], body, actor, db
        )
        repeated = await evaluation_routes.create_evaluation_run(
            fixture["project_id"], body, actor, db
        )
        assert repeated.id == created.id
        assert created.status == "queued"
        assert created.manifest_digest == estimate.manifest_digest
        assert created.manifest["task_inputs"][0]["base_sha"] == "a" * 40
        assert created.sample_size == 5
        assert created.missing_samples == 5

        child = await db.scalar(select(HarnessExperiment).where(
            HarnessExperiment.id == created.experiment_ids[0]
        ))
        run = await db.get(EvaluationRun, created.id)
        assert child is not None and run is not None
        assert child.result["evaluation_manifest_digest"] == run.manifest_digest
        assert child.evaluation_run_id == run.id
        # Every descendant model request must also fit the immutable aggregate budget.
        harnessa_run = await db.get(Run, fixture["run_id"])
        assert harnessa_run is not None
        harnessa_run.execution_result = {"experiment_id": str(child.id)}
        run.max_cost_usd = "0.00000001"
        run.cancel_requested_at = run.created_at
        await db.flush()
        from harnessa.services.model_gateway import (
            ModelGatewayError,
            _reserve,
            route_candidates,
        )

        from tests.integration.test_model_gateway import model_request

        request = model_request(fixture, pool=ModelPoolKind.PROBE)
        pool, candidates = await route_candidates(
            db,
            organization_id=fixture["organization_id"],
            pool_kind=request.pool.value,
            alias_name=request.model_alias,
            requirements=request.requirements,
        )
        with pytest.raises(ModelGatewayError, match="Evaluation spend ceiling"):
            await _reserve(
                db,
                organization_id=fixture["organization_id"],
                project_id=fixture["project_id"],
                task_id=task.id,
                run_id=harnessa_run.id,
                pool=pool,
                candidates=candidates,
                request=request,
                idempotency_key="aggregate-evaluation-budget-test",
            )
        run.max_cost_usd = "3.10500000"
        harnessa_run.execution_result = None
        db.add_all([
            HarnessProbeRun(
                organization_id=fixture["organization_id"], project_id=fixture["project_id"],
                task_id=task.id, experiment_id=child.id, label=f"rollout-{index}",
                phase=phase, status="succeeded", delivered=delivered, wall_ms=duration,
                tokens=100, cost_usd="0.0001",
            )
            for index, (lane, phase, delivered, duration) in enumerate([
                ("shared", "baseline", False, 100),
                ("parallel", "candidate", True, 120),
                ("parallel", "composed", True, 140),
                ("sequential", "candidate", True, 220),
                ("sequential", "composed", True, 240),
            ])
        ])
        await db.flush()
        summarized = await evaluation_routes._read_run(db, run)
        assert summarized.completed_samples == 5
        assert summarized.failed_samples == 0 and summarized.missing_samples == 0
        assert summarized.delivery_samples == 5 and summarized.delivery_successes == 4
        assert summarized.delivery_wilson_95 is not None
        assert summarized.cost_usd == "0.00050000"
        assert summarized.p50_latency_ms == 140 and summarized.p95_latency_ms == 240
        cancelled = await evaluation_routes.cancel_evaluation_run(
            fixture["project_id"], created.id, actor, db
        )
        assert cancelled.status == "cancelled"
        assert child.status == "cancelled"

        with pytest.raises(HTTPException) as conflict:
            await evaluation_routes.create_evaluation_run(
                fixture["project_id"], body.model_copy(update={"mode": "parallel"}), actor, db
            )
        assert conflict.value.status_code == 409

        policy = await db.scalar(select(HarnessPolicy).where(
            HarnessPolicy.project_id == fixture["project_id"]
        ))
        assert policy is not None
