"""Deterministic fake execution adapter for tests only; never installed in production."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from harnessa.adapters.execution import ExecutionFailure, RunInput, TaskInput


class FakeExecutionAdapter:
    name = "test-fake-execution"
    available = True

    def __init__(self, *, mode: str = "success", result: Mapping[str, Any] | None = None) -> None:
        self.mode = mode
        self.result = dict(result or {"fixture": "completed"})
        self.calls: list[tuple[str, int, Mapping[str, Any] | None]] = []
        self.cancelled: list[str] = []
        self.started = asyncio.Event()
        self.cancel_event = asyncio.Event()

    async def execute(
        self,
        task: TaskInput,
        run: RunInput,
        *,
        checkpoint: Mapping[str, Any] | None,
        save_checkpoint: Callable[[Mapping[str, Any]], Awaitable[None]],
        is_cancelled: Callable[[], Awaitable[bool]],
    ) -> Mapping[str, Any]:
        self.calls.append((str(task.id), run.attempt, checkpoint))
        if await is_cancelled():
            return {"cancelled": True}
        if checkpoint is None:
            await save_checkpoint({"fixture_step": 1, "task_id": str(task.id)})
        if self.mode == "transient_failure":
            raise ExecutionFailure("transient", "fixture_transient", "Injected test adapter failure")
        if self.mode == "rate_limited":
            raise ExecutionFailure(
                "rate_limited", "fixture_rate_limit", "Injected test rate limit", retry_after_seconds=5
            )
        if self.mode == "permanent_failure":
            raise ExecutionFailure("permanent", "fixture_permanent", "Injected test permanent failure")
        if self.mode == "wait_for_cancel":
            self.started.set()
            while not self.cancel_event.is_set():
                if await is_cancelled():
                    break
                await asyncio.sleep(0.01)
            return {"cancelled": True}
        return dict(self.result)

    async def cancel(self, run_id) -> None:
        self.cancelled.append(str(run_id))
        self.cancel_event.set()
