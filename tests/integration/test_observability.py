from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from harnessa.db.model_gateway import ModelDeployment, ModelPool
from harnessa.db.models import Task
from harnessa.db.orchestration import Run, RunArtifact, TaskLease
from harnessa.db.session import SessionLocal
from harnessa.observability import OperationalMetrics
from sqlalchemy import select

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_operational_metrics_query_durable_tables_on_empty_database() -> None:
    metrics = await OperationalMetrics()._collect()

    assert metrics["queue_runs"] == 0
    assert metrics["stale_task_leases"] == 0
    assert metrics["dead_letters_open"] == 0
    assert metrics["outbox_unpublished"] == 0
    assert metrics["effects_unreconciled"] == 0
    assert metrics["model_reservations_unreconciled"] == 0
    assert metrics["artifacts"] == 0
    assert metrics["artifact_bytes_created_24h"] == 0
    assert metrics["github_outbox_pending"] == 0
    assert metrics["cleanup_failures"] == 0
    assert metrics["model_route_candidates_available"] == 0


@pytest.mark.anyio
async def test_operational_metrics_report_queue_capacity_and_artifact_growth() -> None:
    fixture = await gateway_fixture()
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        queued_run = Run(
            task_id=fixture["task_id"],
            project_id=fixture["project_id"],
            attempt=2,
            status="QUEUED",
            fencing_token=0,
            adapter_name="test-fixture",
        )
        artifact_digest = "a" * 64
        artifact = RunArtifact(
            organization_id=fixture["organization_id"],
            project_id=fixture["project_id"],
            task_id=fixture["task_id"],
            run_id=fixture["run_id"],
            fencing_token=1,
            digest=artifact_digest,
            media_type="text/plain",
            size_bytes=64,
            storage_key=artifact_digest,
        )
        released_task = Task(
            project_id=fixture["project_id"],
            title="Released lease fixture",
            description="",
        )
        db.add_all([queued_run, artifact, released_task])
        await db.flush()
        db.add_all(
            [
                TaskLease(
                    task_id=fixture["task_id"],
                    run_id=fixture["run_id"],
                    owner_id="expired-worker",
                    fencing_token=2,
                    expires_at=now - timedelta(seconds=5),
                ),
                TaskLease(
                    task_id=released_task.id,
                    owner_id=None,
                    fencing_token=1,
                    expires_at=now - timedelta(seconds=5),
                ),
            ]
        )
        await db.commit()

    metrics = await OperationalMetrics()._collect()

    assert metrics["queue_runs"] == 1
    assert metrics["queue_oldest_seconds"] >= 0
    assert metrics["artifacts"] == 1
    assert metrics["artifact_bytes"] == 64
    assert metrics["artifact_bytes_created_24h"] == 64
    assert metrics["stale_task_leases"] == 1
    assert metrics["model_route_candidates_available"] == 2

    async with SessionLocal() as db:
        pools = (
            await db.execute(
                select(ModelPool).where(ModelPool.organization_id == fixture["organization_id"])
            )
        ).scalars()
        for pool in pools:
            if pool.kind == "interactive":
                pool.active_interactive = pool.max_concurrency
            else:
                pool.active_probe = max(0, pool.max_concurrency - pool.interactive_reserve)
        await db.commit()

    metrics = await OperationalMetrics()._collect()
    assert metrics["model_route_candidates_available"] == 0

    async with SessionLocal() as db:
        deployments = (
            await db.execute(
                select(ModelDeployment).where(
                    ModelDeployment.organization_id == fixture["organization_id"]
                )
            )
        ).scalars()
        pools = (
            await db.execute(
                select(ModelPool).where(ModelPool.organization_id == fixture["organization_id"])
            )
        ).scalars()
        for deployment in deployments:
            deployment.active_interactive = deployment.max_concurrency
        for pool in pools:
            pool.active_interactive = 0
            pool.active_probe = 0
        await db.commit()

    metrics = await OperationalMetrics()._collect()
    assert metrics["model_route_candidates_available"] == 0
