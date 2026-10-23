from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.api.evidence_routes import (
    list_project_evidence,
    reconcile_artifact_integrity,
    reconcile_gateway_usage,
)
from harnessa.contracts import GatewayUsageImportBatch, GatewayUsageObservation
from harnessa.db.github import (
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
    GitHubWebhookDelivery,
)
from harnessa.db.model_gateway import GatewayUsageImport, ModelRequestLedger
from harnessa.db.models import Membership, Project, ProjectMembership, Repository, Task, User
from harnessa.db.orchestration import DomainEvent, EffectIntent, RunArtifact, TaskTransition
from harnessa.db.review import VerificationExecution, VerificationManifestRevision
from harnessa.db.session import SessionLocal
from harnessa.db.specification import (
    SpecificationArtifactRevision,
    SpecificationInterview,
    SpecificationReadinessEvaluation,
)
from harnessa.services.evidence import reconcile_due_gateway_usage_imports
from sqlalchemy import func, select

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_gateway_usage_import_requires_operator_or_administrator():
    fixture = await gateway_fixture()
    observation = GatewayUsageObservation(
        source_observation_id="viewer-cannot-submit",
        request_identifier="untrusted-request",
        observed_at=datetime.now(UTC),
        gateway_billed_usd="1.00",
    )
    async with SessionLocal() as db:
        actor = await db.get(User, fixture["user_id"])
        org_membership = (
            await db.execute(select(Membership).where(Membership.user_id == fixture["user_id"]))
        ).scalar_one()
        project_membership = (
            await db.execute(
                select(ProjectMembership).where(
                    ProjectMembership.user_id == fixture["user_id"],
                    ProjectMembership.project_id == fixture["project_id"],
                )
            )
        ).scalar_one()
        org_membership.role = project_membership.role = "viewer"
        await db.commit()
        assert actor is not None
        with pytest.raises(HTTPException, match="operator or administrator"):
            await reconcile_gateway_usage(
                fixture["project_id"],
                GatewayUsageImportBatch(observations=[observation]),
                actor,
                db,
            )


@pytest.mark.anyio
async def test_provenance_joins_sources_and_enforces_project_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first = await gateway_fixture()
    second = await gateway_fixture()
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        event = DomainEvent(
            event_type="run.tool_started",
            event_version=1,
            schema_version=1,
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            task_id=first["task_id"],
            run_id=first["run_id"],
            aggregate_sequence=1,
            project_sequence=1,
            actor_id=first["user_id"],
            correlation_id=uuid4(),
            causation_id=None,
            payload={"tool_call_id": "tool-call-a", "prompt": "prompt-secret-fixture"},
            retention_until=now + timedelta(days=1),
        )
        model = ModelRequestLedger(
            request_id=uuid4(),
            attempt=2,
            event_type="attempt_failed",
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            task_id=first["task_id"],
            run_id=first["run_id"],
            pool_name="interactive",
            model_alias="fixture-alias",
            actual_model=None,
            request_identifier="gateway-request-fixture",
            route_decision="primary",
            price_revision="fixture-price-1",
            input_tokens=None,
            output_tokens=None,
            usage_known=False,
            latency_ms=10,
            billed_usd=None,
            estimated_usd=None,
            http_status=502,
            error_code="upstream_error",
            details={},
        )
        db.add_all([event, model])
        task = await db.get(Task, first["task_id"])
        project = await db.get(Project, first["project_id"])
        assert task is not None and project is not None
        task.event_sequence = 1
        task.stage = "DONE"
        task.run_status = "SUCCEEDED"
        task.version = 3
        project.event_sequence = 1
        repository = Repository(
            project_id=first["project_id"],
            target_id="fixture/trace",
            display_name="fixture/trace",
            default_branch="main",
        )
        db.add(repository)
        await db.flush()
        interview = SpecificationInterview(
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            task_id=first["task_id"],
            repository_id=repository.id,
            created_by=first["user_id"],
            topic="Deterministic trace",
            change_id="evidence-trace",
            base_sha="a" * 40,
            head_sha="b" * 40,
            idempotency_key="spec-fixture-1",
            request_hash="c" * 64,
            revision=1,
            artifact_revision=1,
            artifact_hash="d" * 64,
            readiness_state="READY",
        )
        db.add(interview)
        await db.flush()
        spec_artifact = SpecificationArtifactRevision(
            interview_id=interview.id,
            revision=1,
            base_sha="a" * 40,
            commit_sha="b" * 40,
            content_hash="d" * 64,
            paths=["openspec/specs/evidence/spec.md"],
            change_summary="Deterministic evidence fixture",
            created_by=first["user_id"],
        )
        db.add(spec_artifact)
        manifest = VerificationManifestRevision(
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            repository_id=repository.id,
            revision=1,
            source_path=".harnessa/verify.yaml",
            source_revision="a" * 40,
            source_digest="e" * 64,
            manifest={"commands": []},
            manifest_digest="f" * 64,
            protected_fixtures={},
            protected_fixture_digest="0" * 64,
            approved_by=first["user_id"],
            approved_at=now,
        )
        db.add(manifest)
        await db.flush()
        verification = VerificationExecution(
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            repository_id=repository.id,
            task_id=first["task_id"],
            run_id=first["run_id"],
            manifest_revision_id=manifest.id,
            attempt=1,
            fencing_token=1,
            source_revision="a" * 40,
            outcome="passed",
            result={"fixture": True},
            content_hash="1" * 64,
        )
        db.add(verification)
        db.add(
            SpecificationReadinessEvaluation(
                interview_id=interview.id,
                interview_revision=1,
                artifact_revision=1,
                content_hash="d" * 64,
                base_sha="a" * 40,
                policy_revision="fixture-policy-v1",
                score=0.95,
                state="READY",
                gates={"fixture": "passed"},
                evaluated_by=first["user_id"],
                signed_off_by=first["user_id"],
            )
        )
        db.add_all(
            [
                TaskTransition(
                    task_id=first["task_id"],
                    project_id=first["project_id"],
                    actor_id=first["user_id"],
                    from_stage="READY",
                    to_stage="IN_PROGRESS",
                    from_run_status="QUEUED",
                    to_run_status="RUNNING",
                    reason="fixture run started",
                    command="transition",
                    command_key="trace-transition-1",
                    request_hash="2" * 64,
                    version_before=1,
                    version_after=2,
                ),
                TaskTransition(
                    task_id=first["task_id"],
                    project_id=first["project_id"],
                    actor_id=first["user_id"],
                    from_stage="IN_PROGRESS",
                    to_stage="DONE",
                    from_run_status="RUNNING",
                    to_run_status="SUCCEEDED",
                    reason="fixture run completed",
                    command="transition",
                    command_key="trace-transition-2",
                    request_hash="3" * 64,
                    version_before=2,
                    version_after=3,
                ),
            ]
        )
        app = GitHubAppConfiguration(
            organization_id=first["organization_id"],
            app_id=1001,
            webhook_id=1002,
            private_key_secret_ref="env:FIXTURE_PRIVATE_KEY",
            webhook_secret_ref="env:FIXTURE_WEBHOOK_SECRET",
        )
        db.add(app)
        await db.flush()
        installation = GitHubInstallation(
            organization_id=first["organization_id"],
            app_configuration_id=app.id,
            installation_id=1003,
            account_login="fixture-org",
            account_type="Organization",
            repository_selection="selected",
            granted_permissions={"contents": "read"},
        )
        db.add(installation)
        await db.flush()
        repository_link = GitHubRepositoryLink(
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            repository_id=repository.id,
            installation_id=installation.id,
            github_repository_id=1004,
            owner="fixture-org",
            name="trace",
            full_name="fixture-org/trace",
            opted_in=True,
            authorized_by=first["user_id"],
            authorized_at=now,
            required_checks=[],
        )
        db.add(repository_link)
        await db.flush()
        db.add_all(
            [
                GitHubSyncObject(
                    organization_id=first["organization_id"],
                    project_id=first["project_id"],
                    repository_link_id=repository_link.id,
                    task_id=first["task_id"],
                    object_kind="pull_request",
                    object_purpose="review",
                    github_object_id="pr-17",
                    object_number=17,
                    stable_key="fixture:pull/17",
                    url="https://github.com/fixture-org/trace/pull/17",
                    state="merged",
                    base_sha="a" * 40,
                    head_sha="b" * 40,
                    check_results={"Local CI": "success"},
                    field_baseline={},
                    sync_conflicts={},
                    reconciled_at=now,
                ),
                GitHubWebhookDelivery(
                    organization_id=first["organization_id"],
                    app_configuration_id=app.id,
                    delivery_id="fixture-delivery-17",
                    event_name="pull_request",
                    action="closed",
                    installation_number=1003,
                    github_repository_id=1004,
                    payload_sha256="4" * 64,
                    payload_reference="sha256:" + "4" * 64,
                    payload={"repository_id": 1004},
                    status="processed",
                    attempts=1,
                    processed_at=now,
                    result={"accepted": True},
                ),
                EffectIntent(
                    project_id=first["project_id"],
                    task_id=first["task_id"],
                    kind="github.pull_request",
                    stable_key="fixture-effect-pr-17",
                    payload={"number": 17},
                    result={"status": 200},
                    status="completed",
                    attempts=1,
                    reconciled_at=now,
                ),
            ]
        )
        store = LocalArtifactStore(tmp_path)
        stored = store.put_bytes(b"authorized artifact fixture", media_type="text/plain")
        artifact = RunArtifact(
            organization_id=first["organization_id"],
            project_id=first["project_id"],
            task_id=first["task_id"],
            run_id=first["run_id"],
            fencing_token=1,
            digest=stored.digest,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.storage_key,
        )
        db.add(artifact)
        await db.commit()
        monkeypatch.setattr("harnessa.api.evidence_routes.settings.artifact_root", str(tmp_path))
        actor = await db.get(User, first["user_id"])
        assert actor is not None
        first_check = await reconcile_artifact_integrity(
            first["project_id"], artifact.id, actor, db
        )
        duplicate_check = await reconcile_artifact_integrity(
            first["project_id"], artifact.id, actor, db
        )
        assert first_check.status == "verified"
        assert duplicate_check.event_id == first_check.event_id
        usage_observation = GatewayUsageObservation(
            source_observation_id="litellm-export-row-001",
            request_identifier="gateway-request-fixture",
            observed_at=now,
            gateway_billed_usd="0.0125",
            provider_reported_usd="0.0110",
            input_tokens=120,
            output_tokens=40,
        )
        unmatched = await reconcile_gateway_usage(
            first["project_id"],
            GatewayUsageImportBatch(observations=[usage_observation]),
            actor,
            db,
        )
        assert unmatched[0].status == "retryable"
        assert unmatched[0].next_retry_at is not None
        async with SessionLocal() as worker_db:
            receipt = (
                await worker_db.execute(
                    select(GatewayUsageImport).where(
                        GatewayUsageImport.project_id == first["project_id"],
                        GatewayUsageImport.source_observation_id == "litellm-export-row-001",
                    )
                )
            ).scalar_one()
            receipt.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            await worker_db.commit()
        db.add(
            ModelRequestLedger(
                request_id=model.request_id,
                attempt=2,
                event_type="attempt_succeeded",
                organization_id=first["organization_id"],
                project_id=first["project_id"],
                task_id=first["task_id"],
                run_id=first["run_id"],
                pool_name="interactive",
                model_alias="fixture-alias",
                actual_model="fixture-model",
                request_identifier="gateway-request-fixture",
                route_decision="primary",
                price_revision="fixture-price-1",
                usage_known=False,
                details={},
            )
        )
        await db.commit()
        assert await reconcile_due_gateway_usage_imports(SessionLocal, limit=100) == 1
        matched = await reconcile_gateway_usage(
            first["project_id"], GatewayUsageImportBatch(observations=[usage_observation]), actor, db
        )
        replayed = await reconcile_gateway_usage(
            first["project_id"],
            GatewayUsageImportBatch(observations=[usage_observation]),
            actor,
            db,
        )
        assert matched[0].status == "matched"
        assert matched[0].ledger_event_id == replayed[0].ledger_event_id
        assert matched[0].ledger_event_id is not None
        imported_event = await db.get(ModelRequestLedger, matched[0].ledger_event_id)
        assert imported_event is not None
        assert imported_event.gateway_usage_source_observation_id == "litellm-export-row-001"

        concurrent_observation = usage_observation.model_copy(
            update={"source_observation_id": "litellm-export-row-concurrent"}
        )

        async def submit_usage():
            async with SessionLocal() as concurrent_db:
                concurrent_actor = await concurrent_db.get(User, first["user_id"])
                assert concurrent_actor is not None
                return await reconcile_gateway_usage(
                    first["project_id"],
                    GatewayUsageImportBatch(observations=[concurrent_observation]),
                    concurrent_actor,
                    concurrent_db,
                )

        from asyncio import gather

        concurrent_results = await gather(submit_usage(), submit_usage())
        assert concurrent_results[0][0].ledger_event_id == concurrent_results[1][0].ledger_event_id
        assert await db.scalar(
            select(func.count()).select_from(ModelRequestLedger).where(
                ModelRequestLedger.gateway_usage_source_observation_id
                == "litellm-export-row-concurrent"
            )
        ) == 1
        project_evidence = await list_project_evidence(
            first["project_id"],
            source="harnessa.operator_gateway_usage_import",
            revision=None,
            cursor=None,
            limit=50,
            actor=actor,
            db=db,
        )
        assert project_evidence.items[0].reconciliation_status == "matched"
        assert project_evidence.items[0].import_status == "operator_submitted_unverified"
        assert project_evidence.items[0].envelope.idempotency_key.value == "litellm-export-row-001"
        for index in range(105):
            sequence = index + 3
            db.add(
                DomainEvent(
                    event_type="fixture.unmatched_revision",
                    event_version=1,
                    schema_version=7 if index == 104 else 1,
                    organization_id=first["organization_id"],
                    project_id=first["project_id"],
                    task_id=first["task_id"],
                    run_id=first["run_id"],
                    aggregate_sequence=sequence,
                    project_sequence=sequence,
                    actor_id=first["user_id"],
                    correlation_id=uuid4(),
                    causation_id=None,
                    payload={},
                    retention_until=now + timedelta(days=1),
                    created_at=now + timedelta(seconds=sequence),
                )
            )
        task.event_sequence = 107
        project.event_sequence = 107
        await db.commit()
        filtered_cursor = None
        sparse_matches = []
        for _ in range(8):
            sparse_page = await list_project_evidence(
                first["project_id"],
                task_id=first["task_id"],
                source="harnessa.domain_event",
                revision="7",
                cursor=filtered_cursor,
                limit=20,
                actor=actor,
                db=db,
            )
            sparse_matches.extend(
                item for item in sparse_page.items if item.summary == "fixture.unmatched_revision"
            )
            filtered_cursor = sparse_page.next_cursor
            if sparse_matches or filtered_cursor is None:
                break
        assert len(sparse_matches) == 1
        page = await list_project_evidence(
            first["project_id"],
            task_id=first["task_id"],
            source=None,
            revision=None,
            cursor=None,
            limit=50,
            actor=actor,
            db=db,
        )
        assert {item.kind for item in page.items} >= {
            "run",
            "domain_event",
            "model_request",
            "specification_revision",
            "evaluation",
            "github_object",
            "webhook_delivery",
            "git_effect",
            "artifact",
        }
        specification = next(item for item in page.items if item.kind == "specification_revision")
        assert "readiness=READY" in specification.summary
        assert "base SHA=" + "a" * 40 in specification.summary
        verification = next(
            item
            for item in page.items
            if item.kind == "evaluation" and item.envelope.source_system == "harnessa.verification"
        )
        assert verification.summary == "verification passed"
        github_object = next(item for item in page.items if item.kind == "github_object")
        assert github_object.envelope.revision.value == "b" * 40
        webhook = next(item for item in page.items if item.kind == "webhook_delivery")
        assert webhook.envelope.webhook_delivery_id.value == "fixture-delivery-17"
        stream = next(item for item in page.items if item.kind == "domain_event")
        assert stream.envelope.stream_cursor.status.value == "known"
        tool = next(item for item in page.items if item.envelope.tool_invocation_id.value)
        assert tool.envelope.tool_invocation_id.value == "tool-call-a"
        model_item = next(item for item in page.items if item.kind == "model_request")
        assert "provider_reported=unknown" in model_item.summary
        assert "gateway_billed=unknown" in model_item.summary
        assert "prompt-secret-fixture" not in str(page.model_dump())
        with pytest.raises(HTTPException) as unauthorized:
            await list_project_evidence(
                second["project_id"],
                source=None,
                revision=None,
                cursor=None,
                limit=50,
                actor=actor,
                db=db,
            )
        assert unauthorized.value.status_code == 403
