"""Execution boundary. Production remains idle until a real adapter is configured."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class TaskInput:
    id: UUID
    project_id: UUID
    title: str
    description: str


@dataclass(frozen=True)
class RunInput:
    id: UUID
    attempt: int
    fencing_token: int


class ExecutionFailure(Exception):
    def __init__(self, retry_class: str, code: str, message: str, retry_after_seconds: float | None = None):
        self.retry_class = retry_class
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class StaleLease(Exception):
    """The worker no longer owns the task's current fencing token."""


class ExecutionAdapter(Protocol):
    name: str
    available: bool

    async def execute(
        self,
        task: TaskInput,
        run: RunInput,
        *,
        checkpoint: Mapping[str, Any] | None,
        save_checkpoint: Callable[[Mapping[str, Any]], Awaitable[None]],
        is_cancelled: Callable[[], Awaitable[bool]],
    ) -> Mapping[str, Any]: ...

    async def cancel(self, run_id: UUID) -> None: ...


class UnconfiguredExecutionAdapter:
    """Safe production default: no work is claimed without a configured executor."""

    name = "unconfigured"
    available = False

    async def execute(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        raise RuntimeError("No production execution adapter is configured")

    async def cancel(self, run_id: UUID) -> None:
        return None
