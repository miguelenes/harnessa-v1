from harnessa.api.task_routes import _allowed_transitions
from harnessa.contracts import Role, TaskStage


def test_transition_capabilities_exclude_command_and_scheduler_owned_targets() -> None:
    allowed = _allowed_transitions(TaskStage.READY, Role.ADMINISTRATOR, "QUEUED")

    assert TaskStage.SPECIFYING in allowed
    assert TaskStage.CLARIFYING in allowed
    assert TaskStage.IN_PROGRESS not in allowed
    assert TaskStage.RETRY_EXHAUSTED not in allowed
    assert TaskStage.CANCELLED not in allowed


def test_in_progress_recovery_is_not_advertised_as_a_transition() -> None:
    allowed = _allowed_transitions(TaskStage.IN_PROGRESS, Role.ADMINISTRATOR, "RUNNING")

    assert TaskStage.READY not in allowed


def test_learning_and_verification_require_successful_run() -> None:
    failed = _allowed_transitions(TaskStage.HUMAN_REVIEW, Role.REVIEWER, "FAILED")
    succeeded = _allowed_transitions(TaskStage.HUMAN_REVIEW, Role.REVIEWER, "SUCCEEDED")

    assert TaskStage.LEARNING not in failed
    assert TaskStage.LEARNING in succeeded
