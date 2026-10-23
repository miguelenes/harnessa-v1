from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from ag_ui.core import (
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
)
from ag_ui.encoder import EventEncoder
from harnessa.adapters.ui_events import project_domain_event


def domain_event(event_type: str, payload: dict[str, object] | None = None) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        event_type=event_type,
        payload=payload or {},
        task_id=uuid4(),
        run_id=uuid4(),
        project_sequence=12,
        aggregate_sequence=4,
        schema_version=1,
        created_at=now,
    )


def test_run_lifecycle_projects_to_standard_ag_ui_events() -> None:
    started = project_domain_event(domain_event("task.run_claimed"))
    finished = project_domain_event(domain_event("run.succeeded"))

    assert isinstance(started, RunStartedEvent)
    assert isinstance(finished, RunFinishedEvent)
    encoded = EventEncoder().encode(started)
    assert '"type":"RUN_STARTED"' in encoded
    assert '"threadId"' in encoded


def test_task_changes_project_to_state_snapshot_with_durable_cursor() -> None:
    projected = project_domain_event(
        domain_event(
            "task.transitioned",
            {"stage": "READY", "version": 9, "secret": "must not reach the UI"},
        )
    )

    assert isinstance(projected, StateSnapshotEvent)
    assert projected.type == EventType.STATE_SNAPSHOT
    assert projected.snapshot["stage"] == "READY"
    assert "secret" not in projected.snapshot
    assert projected.metadata is not None
    assert projected.metadata["harnessa"]["projectCursor"] == 12


def test_unknown_domain_events_are_namespaced_and_do_not_forward_payload() -> None:
    projected = project_domain_event(
        domain_event("future.event", {"tool_output": "private prompt content"})
    )

    encoded = EventEncoder().encode(projected)
    assert '"name":"harnessa.domain_event"' in encoded
    assert "private prompt content" not in encoded


def test_progress_is_an_allowlisted_delta_and_failure_text_is_redacted() -> None:
    progress = project_domain_event(
        domain_event(
            "run.progress",
            {"phase": "agent_updates", "update_counts": {"completed": 2}, "prompt": "private"},
        )
    )
    assert isinstance(progress, StateDeltaEvent)
    assert progress.delta[0].path == "/phase"
    assert "private" not in EventEncoder().encode(progress)

    failed = project_domain_event(domain_event("run.failed", {"code": "worker_error", "secret": "hidden"}))
    assert isinstance(failed, RunErrorEvent)
    encoded = EventEncoder().encode(failed)
    assert "worker_error" in encoded
    assert "hidden" not in encoded
