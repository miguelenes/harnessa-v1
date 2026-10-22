from harnessa.contracts import RunStatus, TaskStage, ErrorResponse
from harnessa.domain.lifecycle import (
    InvalidTaskTransition,
    TaskVersionConflict,
    assert_transition,
)
import pytest


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
