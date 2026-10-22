from __future__ import annotations

from uuid import uuid4

from harnessa.adapters.acp_execution import _safe_recovery_context, _task_prompt
from harnessa.adapters.execution import TaskInput


def test_unsupported_agent_resume_starts_with_bounded_durable_context() -> None:
    task = TaskInput(uuid4(), uuid4(), "Continue", "Implement the requested change")
    previous_run = uuid4()
    checkpoint = {
        "kind": "acp_execution",
        "task_id": str(task.id),
        "run_id": str(previous_run),
        "attempt": 2,
        "base_commit": "a" * 40,
        "phase": "session_update",
        "agent_session_resume_supported": True,
        "update_counts": {"agent_message_chunk": 100, "tool_call": 2},
        "private_reasoning": "must not be passed to the next attempt",
    }

    context = _safe_recovery_context(checkpoint, task)
    prompt = _task_prompt(task, "a" * 40, context)

    assert context == {
        "run_id": str(previous_run),
        "base_commit": "a" * 40,
        "phase": "session_update",
        "attempt": 2,
        "agent_session_resume_supported": True,
        "update_counts": {"agent_message_chunk": 100, "tool_call": 2},
    }
    assert "fresh ACP session" in prompt
    assert "previous agent session was not resumed" in prompt
    assert "must not be passed to the next attempt" not in prompt


def test_recovery_context_rejects_mismatched_task_and_malformed_commit() -> None:
    task = TaskInput(uuid4(), uuid4(), "Continue", "Description")
    mismatched = {
        "kind": "acp_execution",
        "task_id": str(uuid4()),
        "base_commit": "a" * 40,
    }
    malformed = {
        "kind": "acp_execution",
        "task_id": str(task.id),
        "base_commit": "../etc/passwd",
        "phase": ["invalid"],
    }

    assert _safe_recovery_context(mismatched, task) == {}
    assert _safe_recovery_context(malformed, task) == {}
