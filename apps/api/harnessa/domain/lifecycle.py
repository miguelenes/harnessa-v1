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


_TRANSITIONS: dict[TaskStage, frozenset[TaskStage]] = {
    TaskStage.BACKLOG: frozenset({TaskStage.SPECIFYING, TaskStage.REJECTED, TaskStage.CANCELLED}),
    TaskStage.SPECIFYING: frozenset(
        {TaskStage.READY, TaskStage.CLARIFYING, TaskStage.REJECTED, TaskStage.CANCELLED}
    ),
    TaskStage.READY: frozenset(
        {TaskStage.IN_PROGRESS, TaskStage.CLARIFYING, TaskStage.REJECTED, TaskStage.CANCELLED}
    ),
    TaskStage.IN_PROGRESS: frozenset(
        {
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
            TaskStage.HUMAN_REVIEW,
            TaskStage.CLARIFYING,
            TaskStage.RETRY_EXHAUSTED,
            TaskStage.REJECTED,
            TaskStage.CANCELLED,
        }
    ),
    TaskStage.HUMAN_REVIEW: frozenset(
        {TaskStage.LEARNING, TaskStage.IN_PROGRESS, TaskStage.REJECTED, TaskStage.CANCELLED}
    ),
    TaskStage.LEARNING: frozenset({TaskStage.DONE}),
    TaskStage.CLARIFYING: frozenset(
        {TaskStage.SPECIFYING, TaskStage.READY, TaskStage.IN_PROGRESS, TaskStage.CANCELLED}
    ),
    TaskStage.HARNESS_PROBE: frozenset({TaskStage.IN_PROGRESS, TaskStage.CANCELLED}),
    TaskStage.RETRY_EXHAUSTED: frozenset({TaskStage.READY, TaskStage.REJECTED, TaskStage.CANCELLED}),
    TaskStage.REJECTED: frozenset(),
    TaskStage.CANCELLED: frozenset(),
    TaskStage.DONE: frozenset(),
}


def assert_transition(
    current: TaskStage, target: TaskStage, *, expected_version: int, current_version: int
) -> None:
    """Validate optimistic concurrency and lifecycle edge before persistence."""

    if expected_version != current_version:
        raise TaskVersionConflict(expected_version, current_version)
    if target not in _TRANSITIONS[current]:
        raise InvalidTaskTransition(f"Cannot move a task from {current.value} to {target.value}")
