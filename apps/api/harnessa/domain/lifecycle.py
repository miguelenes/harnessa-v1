"""Canonical task lifecycle guards shared by API and worker processes."""

from harnessa.contracts import TaskStage


class InvalidTaskTransition(ValueError):
    """Raised when a requested stage edge is not part of the lifecycle."""


class TaskVersionConflict(ValueError):
    """Raised when a mutation was based on an older task version."""

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"Expected task version {expected}; current version is {current}")


class TransitionGuardFailed(ValueError):
    """Raised when a lifecycle edge is legal only when its domain guard holds."""


_TRANSITIONS: dict[TaskStage, frozenset[TaskStage]] = {
    TaskStage.BACKLOG: frozenset({TaskStage.SPECIFYING, TaskStage.REJECTED, TaskStage.CANCELLED}),
    TaskStage.SPECIFYING: frozenset(
        {TaskStage.READY, TaskStage.CLARIFYING, TaskStage.REJECTED, TaskStage.CANCELLED}
    ),
    TaskStage.READY: frozenset(
        {
            TaskStage.SPECIFYING,
            TaskStage.IN_PROGRESS,
            TaskStage.CLARIFYING,
            TaskStage.RETRY_EXHAUSTED,
            TaskStage.REJECTED,
            TaskStage.CANCELLED,
        }
    ),
    TaskStage.IN_PROGRESS: frozenset(
        {
            TaskStage.READY,  # checkpointed resume/recovery only
            TaskStage.VERIFYING,
            TaskStage.CLARIFYING,
            TaskStage.HARNESS_PROBE,
            TaskStage.RETRY_EXHAUSTED,
            TaskStage.REJECTED,
            TaskStage.CANCELLED,
        }
    ),
    TaskStage.VERIFYING: frozenset(
        {
            TaskStage.IN_PROGRESS,
            TaskStage.READY,  # bounded repair after an independent failed verification
            TaskStage.HUMAN_REVIEW,
            TaskStage.CLARIFYING,
            TaskStage.RETRY_EXHAUSTED,
            TaskStage.REJECTED,
            TaskStage.CANCELLED,
        }
    ),
    TaskStage.HUMAN_REVIEW: frozenset(
        {
            TaskStage.LEARNING,
            TaskStage.IN_PROGRESS,
            TaskStage.READY,
            TaskStage.DONE,
            TaskStage.REJECTED,
            TaskStage.CANCELLED,
        }
    ),
    TaskStage.LEARNING: frozenset({TaskStage.DONE}),
    TaskStage.CLARIFYING: frozenset(
        {TaskStage.SPECIFYING, TaskStage.READY, TaskStage.IN_PROGRESS, TaskStage.CANCELLED}
    ),
    TaskStage.HARNESS_PROBE: frozenset({TaskStage.IN_PROGRESS, TaskStage.CANCELLED}),
    TaskStage.RETRY_EXHAUSTED: frozenset(
        {TaskStage.READY, TaskStage.REJECTED, TaskStage.CANCELLED}
    ),
    TaskStage.REJECTED: frozenset(),
    TaskStage.CANCELLED: frozenset(),
    TaskStage.DONE: frozenset(),
}


def allowed_transition_targets(current: TaskStage) -> frozenset[TaskStage]:
    """Return lifecycle edges before role- and state-specific command checks."""
    return _TRANSITIONS[current]


def assert_transition(
    current: TaskStage,
    target: TaskStage,
    *,
    expected_version: int,
    current_version: int,
    reason: str,
    return_stage: TaskStage | None,
    recovery: bool = False,
) -> None:
    """Validate optimistic concurrency and lifecycle edge before persistence."""

    if expected_version != current_version:
        raise TaskVersionConflict(expected_version, current_version)
    if target not in _TRANSITIONS[current]:
        raise InvalidTaskTransition(f"Cannot move a task from {current.value} to {target.value}")
    if not reason.strip():
        raise TransitionGuardFailed("A transition reason is required")
    if current == TaskStage.IN_PROGRESS and target == TaskStage.READY and not recovery:
        raise TransitionGuardFailed("Returning to READY requires an authorized checkpoint recovery")
    if (
        current == TaskStage.READY
        and target == TaskStage.RETRY_EXHAUSTED
        and "limit" not in reason.lower()
    ):
        raise TransitionGuardFailed("READY can only exhaust through an explicit retry limit")
    if target in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE} and return_stage is None:
        raise TransitionGuardFailed("Branch transitions must preserve a return stage")
    if target in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE} and return_stage != current:
        raise TransitionGuardFailed("A branch must return to the stage it interrupted")
    if (
        current in {TaskStage.CLARIFYING, TaskStage.HARNESS_PROBE}
        and target not in {TaskStage.CANCELLED, TaskStage.REJECTED}
        and target != return_stage
        and not (recovery and target == TaskStage.READY)
        and not (current == TaskStage.CLARIFYING and target == TaskStage.SPECIFYING)
    ):
        raise TransitionGuardFailed("A branch must resume at its recorded return stage")
