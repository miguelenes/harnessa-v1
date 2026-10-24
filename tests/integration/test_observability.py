from __future__ import annotations

import pytest
from harnessa.db.orchestration import Run, RunArtifact
from harnessa.db.session import SessionLocal
from harnessa.observability import OperationalMetrics

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
        db.add_all([queued_run, artifact])
        await db.commit()

    metrics = await OperationalMetrics()._collect()

    assert metrics["queue_runs"] == 1
    assert metrics["queue_oldest_seconds"] >= 0
    assert metrics["artifacts"] == 1
    assert metrics["artifact_bytes"] == 64
    assert metrics["artifact_bytes_created_24h"] == 64
    assert metrics["model_route_candidates_available"] == 2
