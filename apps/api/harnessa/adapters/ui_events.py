"""Project durable domain events into the public AG-UI wire protocol."""

from typing import Any

from ag_ui.core import (
    CustomEvent,
    Event,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
)

from harnessa.db.orchestration import DomainEvent


def project_domain_event(row: DomainEvent) -> Event:
    """Return a bounded, redacted UI projection; PostgreSQL remains authoritative."""
    payload: dict[str, Any] = row.payload if isinstance(row.payload, dict) else {}
    event_type = row.event_type
    metadata = {
        "harnessa": {
            "eventId": str(row.id),
            "projectCursor": row.project_sequence,
            "aggregateSequence": row.aggregate_sequence,
            "schemaVersion": row.schema_version,
        }
    }
    run_id = str(row.run_id or row.task_id or row.id)
    if event_type in {"task.run_claimed", "run.resumed"} and row.task_id:
        return RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=str(row.task_id),
            run_id=run_id,
            timestamp=int(row.created_at.timestamp() * 1000),
            metadata=metadata,
        )
    if event_type in {"run.succeeded", "run.finished"} and row.task_id:
        return RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=str(row.task_id),
            run_id=run_id,
            timestamp=int(row.created_at.timestamp() * 1000),
            metadata=metadata,
        )
    if event_type == "run.failed" and row.task_id:
        error_code = payload.get("code")
        return RunErrorEvent(
            type=EventType.RUN_ERROR,
            timestamp=int(row.created_at.timestamp() * 1000),
            message="The run failed. Inspect the authorized task details for more information.",
            code=str(error_code)[:80] if isinstance(error_code, str) else None,
            metadata=metadata,
        )
    if event_type == "run.progress":
        delta: list[dict[str, object]] = []
        phase = payload.get("phase")
        if isinstance(phase, str):
            delta.append({"op": "add", "path": "/phase", "value": phase[:80]})
        counts = payload.get("update_counts")
        if isinstance(counts, dict):
            safe_counts = {
                key[:80]: value
                for key, value in counts.items()
                if isinstance(key, str) and isinstance(value, int) and 0 <= value <= 1_000_000
            }
            delta.append({"op": "add", "path": "/updateCounts", "value": safe_counts})
        return StateDeltaEvent.model_validate(
            {
                "type": EventType.STATE_DELTA,
                "delta": delta,
                "timestamp": int(row.created_at.timestamp() * 1000),
                "metadata": metadata,
            }
        )
    if event_type in {"task.transitioned", "task.ready", "run.status_changed"}:
        # Never pass through arbitrary payloads (which can contain tool output or secrets).
        safe: dict[str, object] = {
            key: payload[key]
            for key in (
                "to_stage",
                "run_status",
                "blocked_reason",
                "version",
                "command",
                "return_stage",
                "from_run_status",
            )
            if key in payload and isinstance(payload[key], (str, int, bool, type(None)))
        }
        if "stage" in payload and isinstance(payload["stage"], str):
            safe["stage"] = payload["stage"]
        if "to_stage" in safe:
            safe["stage"] = safe.pop("to_stage")
        return StateSnapshotEvent(
            type=EventType.STATE_SNAPSHOT,
            snapshot={"taskId": str(row.task_id), **safe},
            timestamp=int(row.created_at.timestamp() * 1000),
            metadata=metadata,
        )
    return CustomEvent(
        type=EventType.CUSTOM,
        name="harnessa.domain_event",
        value={"eventType": event_type, "taskId": str(row.task_id) if row.task_id else None},
        timestamp=int(row.created_at.timestamp() * 1000),
        metadata=metadata,
    )
