from uuid import uuid4

import pytest
from harnessa.adapters.protocols import RunOrchestrator
from harnessa.contracts import (
    CoreEvidenceEnvelope,
    ErrorResponse,
    EvidenceIntegerValue,
    EvidenceTextValue,
    EvidenceUuidValue,
    EvidenceValueStatus,
    RunOrchestrationIdentity,
    RunReconcileRequest,
    RunStartRequest,
    RunStatus,
    TaskStage,
)
from harnessa.domain.lifecycle import (
    InvalidTaskTransition,
    TaskVersionConflict,
    assert_transition,
)
from pydantic import ValidationError


def test_canonical_lifecycle_and_run_status_are_independent():
    assert TaskStage.IN_PROGRESS.value == "IN_PROGRESS"
    assert TaskStage.HARNESS_PROBE.value == "HARNESS_PROBE"
    assert RunStatus.WAITING_FOR_INPUT.value == "WAITING_FOR_INPUT"
    assert RunStatus.SUCCEEDED.value == "SUCCEEDED"


def test_error_response_contract():
    error = ErrorResponse(
        code="version_conflict", message="Conflict", details={"current_version": 3}
    )
    assert error.details["current_version"] == 3


def test_task_transition_guard_checks_version_and_lifecycle():
    assert_transition(
        TaskStage.BACKLOG,
        TaskStage.SPECIFYING,
        expected_version=1,
        current_version=1,
        reason="specification started",
        return_stage=None,
    )
    with pytest.raises(TaskVersionConflict):
        assert_transition(
            TaskStage.BACKLOG,
            TaskStage.SPECIFYING,
            expected_version=1,
            current_version=2,
            reason="stale",
            return_stage=None,
        )
    with pytest.raises(InvalidTaskTransition):
        assert_transition(
            TaskStage.DONE,
            TaskStage.IN_PROGRESS,
            expected_version=1,
            current_version=1,
            reason="terminal",
            return_stage=None,
        )


def test_orchestration_contract_keeps_harnessa_identity_and_idempotency():
    identity = RunOrchestrationIdentity(
        task_id=uuid4(), run_id=uuid4(), attempt=2, fencing_token=9
    )
    request = RunStartRequest(
        identity=identity,
        idempotency_key="dispatch:stable",
        input={"fixture": "deterministic"},
    )
    assert request.identity.fencing_token == 9
    assert request.resume_from is None
    assert "langgraph" not in str(RunStartRequest.model_json_schema()).lower()
    with pytest.raises(ValidationError):
        RunOrchestrationIdentity(
            task_id=uuid4(), run_id=uuid4(), attempt=1, fencing_token=0
        )

    with pytest.raises(ValidationError):
        RunReconcileRequest(
            identity=identity,
            idempotency_key="reconcile:duplicate",
            stable_effect_keys=["effect:one", "effect:one"],
        )


def test_run_orchestration_port_declares_the_required_neutral_operations():
    for name in ("start", "checkpoint", "interrupt", "answer", "resume", "cancel", "reconcile"):
        assert callable(getattr(RunOrchestrator, name))


def test_core_evidence_distinguishes_unknown_from_redacted_values():
    assert EvidenceTextValue(status=EvidenceValueStatus.UNKNOWN).value is None
    assert EvidenceUuidValue(status=EvidenceValueStatus.REDACTED).value is None
    assert EvidenceIntegerValue(status=EvidenceValueStatus.KNOWN, value=1).value == 1
    with pytest.raises(ValidationError):
        EvidenceTextValue(status=EvidenceValueStatus.KNOWN)
    with pytest.raises(ValidationError):
        EvidenceIntegerValue(status=EvidenceValueStatus.KNOWN, value=0)

    evidence = CoreEvidenceEnvelope(
        schema_version=1,
        source_system="harnessa",
        source_record_id=EvidenceTextValue(status=EvidenceValueStatus.UNKNOWN),
        task_id=EvidenceUuidValue(status=EvidenceValueStatus.KNOWN, value=uuid4()),
        run_id=EvidenceUuidValue(status=EvidenceValueStatus.UNKNOWN),
        attempt=EvidenceIntegerValue(status=EvidenceValueStatus.UNKNOWN),
        revision=EvidenceTextValue(status=EvidenceValueStatus.UNKNOWN),
        content_sha256=EvidenceTextValue(status=EvidenceValueStatus.UNKNOWN),
        occurred_at={"status": "unknown"},
        recorded_at="2026-10-22T00:00:00Z",
        correlation_id=EvidenceUuidValue(status=EvidenceValueStatus.UNKNOWN),
        causation_id=EvidenceUuidValue(status=EvidenceValueStatus.UNKNOWN),
        scope=["task"],
        sensitivity="internal",
        redaction_status="not_required",
    )
    assert evidence.run_id.status == EvidenceValueStatus.UNKNOWN


def test_orchestration_and_evidence_contracts_are_in_canonical_openapi():
    from harnessa.main import app

    schemas = app.openapi()["components"]["schemas"]
    assert "RunOrchestrationIdentity" in schemas
    assert "RunStartRequest" in schemas
    assert "RunOrchestrationReceipt" in schemas
    assert "CoreEvidenceEnvelope" in schemas
