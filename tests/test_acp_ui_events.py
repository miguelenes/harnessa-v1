from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import harnessa.adapters.acp_execution as execution
import pytest
from acp.schema import AgentMessageChunk, ToolCallProgress, ToolCallStart
from harnessa.adapters.acp_session import _SandboxACPClient
from harnessa.adapters.execution import StaleLease


@pytest.mark.anyio
async def test_acp_updates_emit_only_bounded_redacted_ui_content() -> None:
    events: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        events.append(event)

    client = _SandboxACPClient(max_updates=20, on_ui_event=capture)
    await client.start_step()
    await client.session_update(
        "session",
        AgentMessageChunk(
            session_update="agent_message_chunk",
            message_id="provider-message",
            content={"type": "text", "text": "api_"},
        ),
    )
    await client.session_update(
        "session",
        AgentMessageChunk(
            session_update="agent_message_chunk",
            message_id="provider-message",
            content={"type": "text", "text": "key=do-not-store"},
        ),
    )
    await client.session_update(
        "session",
        ToolCallStart(
            tool_call_id="provider-tool",
            title="/private/path",
            kind="read",
            status="in_progress",
            raw_input={"token": "do-not-store"},
            session_update="tool_call",
        ),
    )
    await client.session_update(
        "session",
        ToolCallProgress(
            tool_call_id="provider-tool",
            kind="read",
            status="completed",
            title="contains private output",
            raw_output={"secret": "do-not-store"},
            session_update="tool_call_update",
        ),
    )
    await client.finish_step("completed")

    kinds = [event["kind"] for event in events]
    assert kinds == [
        "step_started",
        "message_started",
        "message_content",
        "message_ended",
        "tool_started",
        "tool_args_redacted",
        "tool_ended",
        "tool_result_redacted",
        "step_finished",
    ]
    serialized = repr(events)
    assert "do-not-store" not in serialized
    assert "/private/path" not in serialized
    content = next(event for event in events if event["kind"] == "message_content")
    assert content["text"] == "Agent message omitted by Harnessa's credential safety policy."
    assert len(str(content["message_id"])) <= 64


@pytest.mark.anyio
async def test_acp_ui_event_buffer_reserves_room_for_open_envelope_closures() -> None:
    events: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        events.append(event)

    client = _SandboxACPClient(max_updates=7, on_ui_event=capture)
    await client.start_step()
    await client.session_update(
        "session",
        AgentMessageChunk(
            session_update="agent_message_chunk",
            message_id="provider-message",
            content={"type": "text", "text": "bounded response"},
        ),
    )
    assert await client._emit_ui_event({"kind": "bounded_progress"})
    assert await client._emit_ui_event({"kind": "bounded_progress"})
    assert not await client._emit_ui_event({"kind": "over_budget_progress"})

    await client.finish_step("failed")

    assert [event["kind"] for event in events] == [
        "step_started", "message_started", "bounded_progress", "bounded_progress",
        "message_content", "message_ended", "step_finished",
    ]
    assert len(events) == client._max_ui_events
    assert client._ui_reserved_events == 0


@pytest.mark.anyio
async def test_cancelled_step_closes_an_interrupted_tool_call() -> None:
    events: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        events.append(event)

    client = _SandboxACPClient(max_updates=20, on_ui_event=capture)
    await client.start_step()
    await client.session_update(
        "session",
        ToolCallStart(
            tool_call_id="provider-tool",
            title="read private file",
            kind="read",
            status="in_progress",
            raw_input={"secret": "never stored"},
            session_update="tool_call",
        ),
    )
    await client.finish_step("cancelled")

    assert [event["kind"] for event in events] == [
        "step_started", "tool_started", "tool_args_redacted", "tool_ended",
        "tool_result_redacted", "step_finished",
    ]
    assert "never stored" not in repr(events)


class FakeEventDatabase:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.commits = 0
        self.locked_entities: list[str] = []

    async def scalar(self, statement: object) -> object:
        entity = statement.column_descriptions[0]["entity"]
        self.locked_entities.append(entity.__name__)
        return self.rows.pop(0)

    async def commit(self) -> None:
        self.commits += 1


class FakeEventSessionFactory:
    def __init__(self, database: FakeEventDatabase) -> None:
        self.database = database

    def __call__(self) -> "FakeEventSessionFactory":
        return self

    async def __aenter__(self) -> FakeEventDatabase:
        return self.database

    async def __aexit__(self, *_args: object) -> None:
        return None


def current_run_rows() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    task_id = uuid4()
    run_id = uuid4()
    project_id = uuid4()
    task = SimpleNamespace(id=task_id, project_id=project_id, run_status="RUNNING")
    run = SimpleNamespace(
        id=run_id,
        task_id=task_id,
        project_id=project_id,
        fencing_token=4,
        status="RUNNING",
    )
    lease = SimpleNamespace(
        task_id=task_id,
        run_id=run_id,
        fencing_token=4,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    return task, run, lease


@pytest.mark.anyio
async def test_normalized_acp_events_append_only_allowlisted_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    task, run, lease = current_run_rows()
    database = FakeEventDatabase([lease, task, run])
    saved: list[dict[str, object]] = []

    async def save_event(_db: object, **kwargs: object) -> None:
        saved.append(kwargs)

    monkeypatch.setattr(execution, "record_domain_event", save_event)
    await execution.persist_acp_ui_event(
        FakeEventSessionFactory(database),
        task=task,
        run=run,
        event={"kind": "tool_started", "tool_call_id": "opaque-id", "tool_name": "private/path"},
    )

    assert saved == [
        {
            "task": task,
            "event_type": "run.tool_started",
            "actor_id": None,
            "run_id": run.id,
            "payload": {"tool_call_id": "opaque-id", "tool_name": "other"},
        }
    ]
    assert database.commits == 1
    assert database.locked_entities == ["TaskLease", "Task", "Run"]


@pytest.mark.anyio
async def test_unrecognized_or_malformed_acp_events_are_not_persisted() -> None:
    task, run, _lease = current_run_rows()

    class UnexpectedSessionFactory:
        def __call__(self) -> object:
            raise AssertionError("invalid UI events must not open a database session")

    factory = UnexpectedSessionFactory()
    await execution.persist_acp_ui_event(
        factory,
        task=task,
        run=run,
        event={"kind": "future_update", "secret": "never persisted"},
    )
    await execution.persist_acp_ui_event(
        factory,
        task=task,
        run=run,
        event={"kind": "message_content", "message_id": "x" * 65, "text": "ignored"},
    )
    await execution.persist_acp_ui_event(
        factory,
        task=task,
        run=run,
        event={"kind": "message_content", "message_id": "valid", "text": "x" * 8193},
    )
    await execution.persist_acp_ui_event(
        factory,
        task=task,
        run=run,
        event={"kind": "message_content", "message_id": "valid", "text": ""},
    )


@pytest.mark.anyio
async def test_acp_event_write_rejects_stale_worker_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    task, run, lease = current_run_rows()
    lease.fencing_token += 1
    database = FakeEventDatabase([lease, task, run])

    async def unexpected_save(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stale events must not be appended")

    monkeypatch.setattr(execution, "record_domain_event", unexpected_save)
    with pytest.raises(StaleLease):
        await execution.persist_acp_ui_event(
            FakeEventSessionFactory(database),
            task=task,
            run=run,
            event={"kind": "step_started"},
        )
    assert database.commits == 0


@pytest.mark.anyio
async def test_cancelled_run_allows_only_terminal_event_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    task, run, lease = current_run_rows()
    task.run_status = "CANCELLED"
    task.cancel_requested_at = datetime.now(UTC)
    database = FakeEventDatabase([lease, task, run])
    saved: list[dict[str, object]] = []

    async def save_event(_db: object, **kwargs: object) -> None:
        saved.append(kwargs)

    monkeypatch.setattr(execution, "record_domain_event", save_event)
    await execution.persist_acp_ui_event(
        FakeEventSessionFactory(database), task=task, run=run,
        event={"kind": "step_finished", "outcome": "cancelled"},
    )
    assert len(saved) == 1
    assert saved[0]["event_type"] == "run.step_finished"
    assert database.locked_entities == ["TaskLease", "Task", "Run"]
    assert database.commits == 1

    # A cancelled run may close an existing AG-UI envelope, but cannot append
    # new assistant or tool activity after cancellation was requested.
    database = FakeEventDatabase([lease, task, run])
    with pytest.raises(StaleLease):
        await execution.persist_acp_ui_event(
            FakeEventSessionFactory(database), task=task, run=run,
            event={"kind": "message_started", "message_id": "opaque"},
        )
    assert database.commits == 0


@pytest.mark.anyio
async def test_paused_run_allows_fenced_terminal_event_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    task, run, lease = current_run_rows()
    task.run_status = "PAUSED"
    run.cancel_requested_at = datetime.now(UTC)
    database = FakeEventDatabase([lease, task, run])

    async def save_event(_db: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(execution, "record_domain_event", save_event)
    await execution.persist_acp_ui_event(
        FakeEventSessionFactory(database), task=task, run=run,
        event={"kind": "step_finished", "outcome": "cancelled"},
    )
    assert database.commits == 1


@pytest.mark.anyio
async def test_cancelled_step_closes_open_message_without_publishing_partial_text() -> None:
    events: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        events.append(event)

    client = _SandboxACPClient(max_updates=20, on_ui_event=capture)
    await client.start_step()
    await client.session_update(
        "session",
        AgentMessageChunk(
            session_update="agent_message_chunk",
            message_id="provider-message",
            content={"type": "text", "text": "partial secret-like response"},
        ),
    )
    await client.finish_step("cancelled")

    assert [event["kind"] for event in events] == [
        "step_started", "message_started", "message_ended", "step_finished"
    ]
    assert "partial secret-like response" not in repr(events)


@pytest.mark.anyio
async def test_prompt_propagates_final_event_persistence_failure() -> None:
    async def reject_event(_event: object) -> None:
        raise RuntimeError("durable event write failed")

    class FakeConnection:
        async def prompt(self, *_args: object) -> SimpleNamespace:
            return SimpleNamespace(stop_reason="completed")

    session = object.__new__(execution.ACPAgentSession)
    session.session_id = "opaque-session"
    session.client = _SandboxACPClient(max_updates=20, on_ui_event=reject_event)
    session.connection = FakeConnection()

    with pytest.raises(RuntimeError, match="durable event write failed"):
        await session.prompt("work", timeout_seconds=1)
