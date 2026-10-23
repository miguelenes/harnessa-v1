"""Project-authorized read projection for cross-system provenance."""

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    CoreEvidenceEnvelope,
    EvidenceIntegerValue,
    EvidenceIntegrityRead,
    EvidencePage,
    EvidenceRecordRead,
    EvidenceRedactionStatus,
    EvidenceSensitivity,
    EvidenceTextValue,
    EvidenceTimestampValue,
    EvidenceUuidValue,
    EvidenceValueStatus,
    GatewayUsageImportBatch,
    GatewayUsageImportResult,
)
from harnessa.db.github import (
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
    GitHubWebhookDelivery,
)
from harnessa.db.model_gateway import GatewayUsageImport, ModelRequestLedger
from harnessa.db.models import Project, Task, User
from harnessa.db.orchestration import DomainEvent, EffectIntent, Run, RunArtifact, TaskTransition
from harnessa.db.retrieval import MCPToolAudit
from harnessa.db.review import VerificationExecution
from harnessa.db.session import get_db
from harnessa.db.specification import (
    SpecificationArtifactRevision,
    SpecificationInterview,
    SpecificationReadinessEvaluation,
)
from harnessa.services.authorization import require_membership
from harnessa.services.evidence import reconcile_gateway_usage_receipt
from harnessa.services.lifecycle import record_domain_event
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

router = APIRouter(tags=["evidence provenance"])


@router.post(
    "/projects/{project_id}/evidence/gateway-usage/reconcile",
    response_model=list[GatewayUsageImportResult],
)
async def reconcile_gateway_usage(
    project_id: UUID,
    batch: GatewayUsageImportBatch,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[GatewayUsageImportResult]:
    """Import bounded gateway usage metadata from an authorized operator."""
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Gateway usage import requires operator or administrator access")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    result: list[GatewayUsageImportResult] = []
    now = datetime.now(UTC)
    for observation in batch.observations:
        if observation.observed_at.tzinfo is None or observation.observed_at.utcoffset() is None:
            raise HTTPException(422, "observed_at must include a timezone")
        await db.execute(
            pg_insert(GatewayUsageImport)
            .values(
                organization_id=project.organization_id,
                project_id=project_id,
                submitted_by=actor.id,
                source_observation_id=observation.source_observation_id,
                request_identifier=observation.request_identifier,
                gateway_billed_usd=observation.gateway_billed_usd,
                provider_reported_usd=observation.provider_reported_usd,
                input_tokens=observation.input_tokens,
                output_tokens=observation.output_tokens,
                usage_observed_at=observation.observed_at,
                status="retryable",
                attempts=0,
            )
            .on_conflict_do_nothing(constraint="uq_gateway_usage_source_observation")
        )
        receipt = (
            await db.execute(
                select(GatewayUsageImport)
                .where(
                    GatewayUsageImport.project_id == project_id,
                    GatewayUsageImport.source_observation_id == observation.source_observation_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        same = (
            receipt.request_identifier == observation.request_identifier
            and receipt.gateway_billed_usd == observation.gateway_billed_usd
            and receipt.provider_reported_usd == observation.provider_reported_usd
            and receipt.input_tokens == observation.input_tokens
            and receipt.output_tokens == observation.output_tokens
            and receipt.usage_observed_at == observation.observed_at
        )
        if not same:
            raise HTTPException(409, "Observation ID was already used for different metadata")
        if receipt.status == "matched" or (
            receipt.status == "retryable"
            and receipt.next_retry_at is not None
            and receipt.next_retry_at > now
        ):
            result.append(
                GatewayUsageImportResult(
                    source_observation_id=receipt.source_observation_id,
                    status=receipt.status,
                    attempts=receipt.attempts,
                    ledger_event_id=receipt.ledger_event_id,
                    next_retry_at=receipt.next_retry_at,
                )
            )
            continue
        if receipt.status in {"failed", "ambiguous"}:
            receipt.status = "retryable"
            receipt.attempts = 0
        await reconcile_gateway_usage_receipt(db, receipt, now=now)
        result.append(
            GatewayUsageImportResult(
                source_observation_id=receipt.source_observation_id,
                status=receipt.status,
                attempts=receipt.attempts,
                ledger_event_id=receipt.ledger_event_id,
                next_retry_at=receipt.next_retry_at,
            )
        )
    await db.commit()
    return result

def _known_uuid(value: UUID | None) -> EvidenceUuidValue:
    return (
        EvidenceUuidValue(status=EvidenceValueStatus.KNOWN, value=value)
        if value
        else EvidenceUuidValue(status=EvidenceValueStatus.UNKNOWN)
    )


def _known_text(value: str | None) -> EvidenceTextValue:
    return (
        EvidenceTextValue(status=EvidenceValueStatus.KNOWN, value=value)
        if value
        else EvidenceTextValue(status=EvidenceValueStatus.UNKNOWN)
    )


def _cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        timestamp, identifier = decoded.split("|", 1)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return parsed, UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Invalid evidence cursor") from exc


def _cursor_text(created_at: datetime, identifier: UUID) -> str:
    raw = f"{created_at.isoformat()}|{identifier}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@router.post(
    "/projects/{project_id}/evidence/artifacts/{artifact_id}/reconcile",
    response_model=EvidenceIntegrityRead,
)
async def reconcile_artifact_integrity(
    project_id: UUID,
    artifact_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EvidenceIntegrityRead:
    """Check one authorized artifact's CAS bytes and append an audit event."""
    await require_membership(db, actor, project_id)
    artifact = await db.get(RunArtifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(404, "Artifact not found")
    status = await run_in_threadpool(
        LocalArtifactStore(Path(settings.artifact_root)).verify_reference,
        artifact.storage_key,
        expected_digest=artifact.digest,
        expected_size=artifact.size_bytes,
    )
    checked_at = datetime.now(UTC)
    task = await db.get(Task, artifact.task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    event_payload = {
        "artifact_id": str(artifact.id),
        "expected_sha256": artifact.digest,
        "expected_size_bytes": artifact.size_bytes,
        "status": status,
    }
    prior_events = (
        (
            await db.execute(
                select(DomainEvent)
                .where(
                    DomainEvent.task_id == task.id,
                    DomainEvent.event_type == "evidence.artifact_integrity_checked",
                    DomainEvent.payload["artifact_id"].as_string() == str(artifact.id),
                )
                .order_by(DomainEvent.project_sequence.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    event = next(
        (
            item
            for item in prior_events
            if all(item.payload.get(key) == value for key, value in event_payload.items())
        ),
        None,
    )
    if event is None:
        event = await record_domain_event(
            db,
            task=task,
            event_type="evidence.artifact_integrity_checked",
            actor_id=actor.id,
            run_id=artifact.run_id,
            payload=event_payload,
        )
        await db.commit()
    return EvidenceIntegrityRead(
        artifact_id=artifact.id,
        expected_sha256=artifact.digest,
        expected_size_bytes=artifact.size_bytes,
        status=status,
        checked_at=checked_at,
        event_id=event.id,
    )


def _record(
    *,
    source: str,
    source_id: str,
    kind: str,
    summary: str,
    recorded_at: datetime,
    organization_id: UUID | None,
    project_id: UUID,
    task_id: UUID | None,
    run_id: UUID | None = None,
    attempt: int | None = None,
    revision: str | None = None,
    content_hash: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    model_request_id: UUID | None = None,
    repository_id: UUID | None = None,
    artifact_id: UUID | None = None,
    evaluation_id: UUID | None = None,
    idempotency_key: str | None = None,
    tool_invocation_id: str | None = None,
    webhook_delivery_id: str | None = None,
    harness_revision: str | None = None,
    stream_cursor: int | None = None,
    stale: bool = False,
    occurred_at: datetime | None = None,
    import_status: str = "native",
    reconciliation_status: str = "not_required",
) -> EvidenceRecordRead:
    unknown_time = EvidenceTimestampValue(status=EvidenceValueStatus.UNKNOWN)
    return EvidenceRecordRead(
        kind=kind,
        summary=summary,
        stale=stale,
        import_status=import_status,
        reconciliation_status=reconciliation_status,
        envelope=CoreEvidenceEnvelope(
            schema_version=2,
            source_system=source,
            source_record_id=_known_text(source_id),
            organization_id=_known_uuid(organization_id),
            project_id=_known_uuid(project_id),
            repository_id=_known_uuid(repository_id),
            task_id=_known_uuid(task_id),
            task_revision=_known_text(revision if kind == "task_transition" else None),
            run_id=_known_uuid(run_id),
            attempt=EvidenceIntegerValue(status=EvidenceValueStatus.KNOWN, value=attempt)
            if attempt is not None
            else EvidenceIntegerValue(status=EvidenceValueStatus.UNKNOWN),
            model_request_id=_known_uuid(model_request_id),
            tool_invocation_id=_known_text(tool_invocation_id),
            webhook_delivery_id=_known_text(webhook_delivery_id),
            git_effect_id=_known_text(source_id if kind == "git_effect" else None),
            artifact_id=_known_uuid(artifact_id),
            evaluation_id=_known_uuid(evaluation_id),
            harness_revision=_known_text(harness_revision),
            skill_revision=_known_text(None),
            revision=_known_text(revision),
            content_sha256=_known_text(content_hash),
            stream_cursor=(
                EvidenceIntegerValue(status=EvidenceValueStatus.KNOWN, value=stream_cursor)
                if stream_cursor is not None and stream_cursor > 0
                else EvidenceIntegerValue(status=EvidenceValueStatus.UNKNOWN)
            ),
            occurred_at=(
                EvidenceTimestampValue(status=EvidenceValueStatus.KNOWN, value=occurred_at)
                if occurred_at is not None
                else unknown_time
            ),
            recorded_at=recorded_at,
            correlation_id=_known_uuid(correlation_id),
            causation_id=_known_uuid(causation_id),
            scope=["organization", "project"],
            idempotency_key=_known_text(idempotency_key),
            sensitivity=EvidenceSensitivity.INTERNAL,
            redaction_status=EvidenceRedactionStatus.APPLIED,
        ),
    )


@router.get("/projects/{project_id}/evidence", response_model=EvidencePage)
async def list_project_evidence(
    project_id: UUID,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
    source: str | None = Query(default=None, max_length=120),
    revision: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> EvidencePage:
    await require_membership(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if task_id is not None:
        task = await db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(404, "Task not found")
    if run_id is not None:
        selected_run = await db.get(Run, run_id)
        if selected_run is None or selected_run.project_id != project_id:
            raise HTTPException(404, "Run not found")
        if task_id is not None and selected_run.task_id != task_id:
            raise HTTPException(404, "Run not found")
        task_id = selected_run.task_id
    boundary = _cursor(cursor)
    scan_boundaries: list[tuple[datetime, UUID]] = []
    source_scan_models: dict[str, set[type[Any]]] = {
        "harnessa.domain_event": {DomainEvent},
        "harnessa.run": {Run},
        "harnessa.task_transition": {TaskTransition},
        "harnessa.specification": {SpecificationArtifactRevision},
        "harnessa.specification_readiness": {SpecificationReadinessEvaluation},
        "litellm.gateway_ledger": {ModelRequestLedger},
        "harnessa.operator_gateway_usage_import": {ModelRequestLedger, GatewayUsageImport},
        "harnessa.artifact_store": {RunArtifact},
        "harnessa.verification": {VerificationExecution},
        "harnessa.github_effect": {EffectIntent},
        "github.reconciled_object": {GitHubSyncObject},
        "harnessa.mcp_audit": {MCPToolAudit},
        "github.webhook_delivery": {GitHubWebhookDelivery},
    }
    requested_source_models: set[type[Any]] | None = (
        source_scan_models.get(source) if source else None
    )

    async def fetch(model: Any, *filters: Any) -> list[Any]:
        conditions = [model.project_id == project_id, *filters]
        if boundary:
            timestamp, identifier = boundary
            conditions.append(
                or_(
                    model.created_at > timestamp,
                    (model.created_at == timestamp) & (model.id > identifier),
                )
            )
        fetched = list(
            (
                await db.execute(
                    select(model)
                    .where(*conditions)
                    .order_by(model.created_at, model.id)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(fetched) > limit and (
            requested_source_models is None or model in requested_source_models
        ):
            scan_boundaries.append((fetched[-1].created_at, fetched[-1].id))
        return fetched

    async def fetch_specification_child(model: Any) -> list[Any]:
        conditions = [SpecificationInterview.project_id == project_id]
        if task_id:
            conditions.append(SpecificationInterview.task_id == task_id)
        if boundary:
            timestamp, identifier = boundary
            conditions.append(
                or_(
                    model.created_at > timestamp,
                    (model.created_at == timestamp) & (model.id > identifier),
                )
            )
        fetched = list(
            (
                await db.execute(
                    select(model)
                    .join(SpecificationInterview, model.interview_id == SpecificationInterview.id)
                    .where(*conditions)
                    .order_by(model.created_at, model.id)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(fetched) > limit and (
            requested_source_models is None or model in requested_source_models
        ):
            scan_boundaries.append((fetched[-1].created_at, fetched[-1].id))
        return fetched

    rows: list[EvidenceRecordRead] = []

    def task_filters(model: Any) -> list[Any]:
        return [model.task_id == task_id] if task_id else []

    events = await fetch(
        DomainEvent,
        DomainEvent.retention_until > datetime.now(UTC),
        *([DomainEvent.task_id == task_id] if task_id else []),
        *([DomainEvent.run_id == run_id] if run_id else []),
    )
    for row in events:
        event_status = row.payload.get("status")
        artifact_id_value = row.payload.get("artifact_id")
        rows.append(
            _record(
                source="harnessa.domain_event",
                source_id=str(row.id),
                kind="domain_event",
                summary=(
                    f"artifact integrity {event_status}"
                    if row.event_type == "evidence.artifact_integrity_checked"
                    and isinstance(event_status, str)
                    else row.event_type
                ),
                recorded_at=row.created_at,
                occurred_at=row.created_at,
                organization_id=row.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                run_id=row.run_id,
                revision=str(row.schema_version),
                correlation_id=row.correlation_id,
                causation_id=row.causation_id,
                tool_invocation_id=(
                    row.payload.get("tool_call_id")
                    if isinstance(row.payload.get("tool_call_id"), str)
                    else None
                ),
                stream_cursor=row.project_sequence,
                artifact_id=(
                    UUID(artifact_id_value)
                    if isinstance(artifact_id_value, str)
                    and row.event_type == "evidence.artifact_integrity_checked"
                    else None
                ),
                content_hash=(
                    row.payload.get("expected_sha256")
                    if isinstance(row.payload.get("expected_sha256"), str)
                    else None
                ),
                reconciliation_status=(
                    str(event_status)
                    if row.event_type == "evidence.artifact_integrity_checked"
                    and isinstance(event_status, str)
                    else "not_required"
                ),
            )
        )
    runs = await fetch(Run, *task_filters(Run), *([Run.id == run_id] if run_id else []))
    latest_run_by_task = {
        task.id: task for task in sorted(runs, key=lambda item: (item.created_at, str(item.id)))
    }
    for row in runs:
        rows.append(
            _record(
                source="harnessa.run",
                source_id=str(row.id),
                kind="run",
                summary=f"{row.status}; base SHA={row.base_sha or 'unknown'}; specification revision={row.specification_revision or 'unknown'}; specification SHA={row.specification_hash or 'unknown'}; specification commit={row.specification_commit_sha or 'unknown'}",
                recorded_at=row.created_at,
                organization_id=project.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                run_id=row.id,
                attempt=row.attempt,
                revision=row.source_revision or row.base_sha,
                content_hash=row.specification_hash,
                harness_revision=row.harness_revision,
            )
        )
    transitions = await fetch(TaskTransition, *task_filters(TaskTransition))
    for row in transitions:
        rows.append(
            _record(
                source="harnessa.task_transition",
                source_id=str(row.id),
                kind="task_transition",
                summary=f"{row.from_stage or 'created'} → {row.to_stage}",
                recorded_at=row.created_at,
                organization_id=project.organization_id,
                project_id=project_id,
                task_id=row.task_id,
                revision=str(row.version_after),
                idempotency_key=row.command_key,
            )
        )
    artifact_revisions = await fetch_specification_child(SpecificationArtifactRevision)
    readiness_rows = await fetch_specification_child(SpecificationReadinessEvaluation)
    interview_ids = {row.interview_id for row in (*artifact_revisions, *readiness_rows)}
    interviews_by_id = {}
    if interview_ids:
        interviews = (
            await db.execute(
                select(SpecificationInterview).where(
                    SpecificationInterview.project_id == project_id,
                    SpecificationInterview.id.in_(interview_ids),
                )
            )
        ).scalars()
        interviews_by_id = {row.id: row for row in interviews}
    for artifact in artifact_revisions:
        interview = interviews_by_id.get(artifact.interview_id)
        if interview is None:
            continue
        rows.append(
            _record(
                source="harnessa.specification",
                source_id=str(artifact.id),
                kind="specification_revision",
                summary=f"revision {artifact.revision}; readiness={interview.readiness_state if artifact.revision == interview.artifact_revision else 'stale'}; base SHA={artifact.base_sha}; commit SHA={artifact.commit_sha}",
                recorded_at=artifact.created_at,
                organization_id=interview.organization_id,
                project_id=interview.project_id,
                task_id=interview.task_id,
                revision=str(artifact.revision),
                content_hash=artifact.content_hash,
                repository_id=interview.repository_id,
                artifact_id=artifact.id,
                stale=artifact.revision != interview.artifact_revision,
            )
        )
    for readiness in readiness_rows:
        interview = interviews_by_id.get(readiness.interview_id)
        if interview is None:
            continue
        rows.append(
            _record(
                source="harnessa.specification_readiness",
                source_id=str(readiness.id),
                kind="evaluation",
                summary=f"specification readiness {readiness.state}; base SHA={readiness.base_sha}; policy={readiness.policy_revision}",
                recorded_at=readiness.created_at,
                organization_id=interview.organization_id,
                project_id=interview.project_id,
                task_id=interview.task_id,
                revision=str(readiness.artifact_revision),
                content_hash=readiness.content_hash,
                evaluation_id=readiness.id,
                repository_id=interview.repository_id,
            )
        )
    ledger = await fetch(
        ModelRequestLedger,
        *task_filters(ModelRequestLedger),
        *([ModelRequestLedger.run_id == run_id] if run_id else []),
        *(
            [ModelRequestLedger.event_type == "usage_reconciled"]
            if source == "harnessa.operator_gateway_usage_import"
            else [ModelRequestLedger.event_type != "usage_reconciled"]
            if source == "litellm.gateway_ledger"
            else []
        ),
    )
    for row in ledger:
        imported_usage = row.event_type == "usage_reconciled"
        source_system = (
            "harnessa.operator_gateway_usage_import"
            if imported_usage
            else "litellm.gateway_ledger"
        )
        detail = f"{row.event_type}; model={row.actual_model or row.model_alias}; route={row.route_decision}; gateway_request={row.request_identifier or 'unknown'}; usage={'known' if row.usage_known else 'unknown'}; gateway_billed={row.billed_usd if row.billed_usd is not None else 'unknown'}; provider_reported={row.provider_reported_usd if row.provider_reported_usd is not None else 'unknown'}; estimate={row.estimated_usd if row.estimated_usd is not None else 'unknown'}"
        rows.append(
            _record(
                source=source_system,
                source_id=str(row.id),
                kind="model_request",
                summary=detail,
                recorded_at=row.created_at,
                organization_id=row.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                run_id=row.run_id,
                attempt=row.attempt or None,
                revision=row.price_revision,
                model_request_id=row.request_id,
                idempotency_key=(
                    row.details.get("source_observation_id")
                    if isinstance(row.details.get("source_observation_id"), str)
                    else None
                ),
                import_status="operator_submitted" if imported_usage else "gateway_recorded",
                reconciliation_status=(
                    "matched_operator_report"
                    if imported_usage
                    else "pending_gateway_usage"
                    if not row.usage_known
                    else "recorded"
                ),
            )
        )
    artifacts = await fetch(
        RunArtifact, *task_filters(RunArtifact), *([RunArtifact.run_id == run_id] if run_id else [])
    )
    for row in artifacts:
        rows.append(
            _record(
                source="harnessa.artifact_store",
                source_id=str(row.id),
                kind="artifact",
                summary=f"{row.media_type}; {row.size_bytes} bytes",
                recorded_at=row.created_at,
                organization_id=row.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                run_id=row.run_id,
                content_hash=row.digest,
                artifact_id=row.id,
                reconciliation_status="checksum_unverified",
            )
        )
    checks = await fetch(
        VerificationExecution,
        *task_filters(VerificationExecution),
        *([VerificationExecution.run_id == run_id] if run_id else []),
    )
    for row in checks:
        rows.append(
            _record(
                source="harnessa.verification",
                source_id=str(row.id),
                kind="evaluation",
                summary=f"verification {row.outcome}",
                recorded_at=row.created_at,
                organization_id=row.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                run_id=row.run_id,
                attempt=row.attempt,
                revision=row.source_revision,
                content_hash=row.content_hash,
                evaluation_id=row.id,
            )
        )
    effects = await fetch(EffectIntent, *task_filters(EffectIntent))
    for row in effects:
        if row.kind.startswith("github"):
            rows.append(
                _record(
                    source="harnessa.github_effect",
                    source_id=str(row.id),
                    kind="git_effect",
                    summary=f"{row.kind}: {row.status}",
                    recorded_at=row.created_at,
                    organization_id=project.organization_id,
                    project_id=project_id,
                    task_id=row.task_id,
                    idempotency_key=row.stable_key,
                )
            )
    github_objects = await fetch(GitHubSyncObject, *task_filters(GitHubSyncObject))
    for row in github_objects:
        rows.append(
            _record(
                source="github.reconciled_object",
                source_id=str(row.id),
                kind="github_object",
                summary=f"{row.object_kind} #{row.object_number or row.github_object_id}: {row.state}",
                recorded_at=row.created_at,
                organization_id=row.organization_id,
                project_id=row.project_id,
                task_id=row.task_id,
                revision=row.head_sha,
                stale=(
                    row.head_sha is not None
                    and row.task_id in latest_run_by_task
                    and latest_run_by_task[row.task_id].source_revision is not None
                    and latest_run_by_task[row.task_id].source_revision != row.head_sha
                ),
            )
        )
    if task_id is None:
        mcp_rows = await fetch(MCPToolAudit)
        for row in mcp_rows:
            rows.append(
                _record(
                    source="harnessa.mcp_audit",
                    source_id=str(row.id),
                    kind="tool_invocation",
                    summary=f"{row.server_id}/{row.tool_name}: {row.outcome}; request sha={row.request_hash}; response sha={row.response_hash or 'unknown'}",
                    recorded_at=row.created_at,
                    organization_id=row.organization_id,
                    project_id=row.project_id,
                    task_id=None,
                    revision=row.protocol_version,
                    content_hash=row.request_hash,
                    tool_invocation_id=str(row.id),
                )
            )
    project_links = list(
        (
            await db.execute(
                select(GitHubRepositoryLink).where(
                    GitHubRepositoryLink.project_id == project_id,
                    *(
                        [
                            GitHubRepositoryLink.id.in_(
                                select(GitHubSyncObject.repository_link_id).where(
                                    GitHubSyncObject.task_id == task_id,
                                    GitHubSyncObject.project_id == project_id,
                                )
                            )
                        ]
                        if task_id
                        else []
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if project_links:
        installation_ids = [item.installation_id for item in project_links]
        config_ids = list(
            (
                await db.execute(
                    select(GitHubInstallation.app_configuration_id).where(
                        GitHubInstallation.id.in_(installation_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        delivery_query = select(GitHubWebhookDelivery).where(
            GitHubWebhookDelivery.app_configuration_id.in_(config_ids),
            GitHubWebhookDelivery.github_repository_id.in_(
                [item.github_repository_id for item in project_links]
            ),
        )
        if boundary:
            timestamp, identifier = boundary
            delivery_query = delivery_query.where(
                or_(
                    GitHubWebhookDelivery.created_at > timestamp,
                    (GitHubWebhookDelivery.created_at == timestamp)
                    & (GitHubWebhookDelivery.id > identifier),
                )
            )
        deliveries = list(
            (
                await db.execute(
                    delivery_query.order_by(
                        GitHubWebhookDelivery.created_at, GitHubWebhookDelivery.id
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(deliveries) > limit:
            scan_boundaries.append((deliveries[-1].created_at, deliveries[-1].id))
        for row in deliveries:
            rows.append(
                _record(
                    source="github.webhook_delivery",
                    source_id=str(row.id),
                    kind="webhook_delivery",
                    summary=f"{row.event_name}{':' + row.action if row.action else ''}: {row.status}; attempts={row.attempts}",
                    recorded_at=row.created_at,
                    organization_id=row.organization_id,
                    project_id=project_id,
                    task_id=task_id,
                    content_hash=row.payload_sha256,
                    webhook_delivery_id=row.delivery_id,
                    import_status="webhook_inbox",
                    reconciliation_status=("retryable" if row.status == "failed" else "recorded"),
                    repository_id=next(
                        (
                            item.repository_id
                            for item in project_links
                            if item.github_repository_id == row.github_repository_id
                        ),
                        None,
                    ),
                )
            )
    if task_id is None:
        usage_imports = await fetch(GatewayUsageImport)
        for row in usage_imports:
            rows.append(
                _record(
                    source="harnessa.operator_gateway_usage_import",
                    source_id=str(row.id),
                    kind="usage_import",
                    summary=(
                        f"operator-submitted unverified gateway report; {row.status}; request={row.request_identifier}; attempts={row.attempts}; "
                        f"gateway_billed={row.gateway_billed_usd if row.gateway_billed_usd is not None else 'unknown'}; "
                        f"provider_reported={row.provider_reported_usd if row.provider_reported_usd is not None else 'unknown'}; "
                        f"next_retry={row.next_retry_at.isoformat() if row.next_retry_at else 'none'}"
                    ),
                    recorded_at=row.created_at,
                    occurred_at=row.usage_observed_at,
                    organization_id=row.organization_id,
                    project_id=project_id,
                    task_id=None,
                    run_id=None,
                    idempotency_key=row.source_observation_id,
                    import_status="operator_submitted_unverified",
                    reconciliation_status=row.status,
                )
            )
    if source:
        rows = [row for row in rows if row.envelope.source_system == source]
    if revision:
        rows = [
            row
            for row in rows
            if row.envelope.revision.value == revision
            or row.envelope.content_sha256.value == revision
        ]
    rows.sort(
        key=lambda item: (item.envelope.recorded_at, str(item.envelope.source_record_id.value))
    )
    if (source or revision) and scan_boundaries:
        scanned_through = min(scan_boundaries)
        scanned_rows = [
            item
            for item in rows
            if (item.envelope.recorded_at, UUID(item.envelope.source_record_id.value))
            <= scanned_through
        ]
        page = scanned_rows[:limit]
        if len(scanned_rows) > limit:
            next_cursor = _cursor_text(
                page[-1].envelope.recorded_at,
                UUID(page[-1].envelope.source_record_id.value),
            )
        else:
            next_cursor = _cursor_text(*scanned_through)
    else:
        page = rows[:limit]
        more = len(rows) > limit
        next_cursor = None
        if more and page:
            next_cursor = _cursor_text(
                page[-1].envelope.recorded_at,
                UUID(page[-1].envelope.source_record_id.value),
            )
    await require_membership(db, actor, project_id)
    return EvidencePage(items=page, next_cursor=next_cursor, limit=limit)
