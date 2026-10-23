from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from harnessa.api import task_routes
from harnessa.config import settings
from harnessa.contracts import TaskCreate
from harnessa.db.models import Membership, ProjectMembership
from harnessa.db.session import SessionLocal
from sqlalchemy import delete, func, select, update

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_task_capabilities_follow_current_project_role_and_run_state() -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        task = await db.get(task_routes.Task, data["task_id"])
        assert actor is not None and task is not None

        operator = await task_routes.task_capabilities(task.id, actor, db)
        assert {item.value for item in operator.allowed_commands} == {"pause", "cancel"}
        assert operator.can_answer_questions and operator.can_edit_artifacts

        membership = await db.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == data["project_id"],
                ProjectMembership.user_id == actor.id,
            )
        )
        organization_membership = await db.scalar(
            select(Membership).where(
                Membership.organization_id == data["organization_id"],
                Membership.user_id == actor.id,
            )
        )
        assert membership is not None and organization_membership is not None
        membership.role = "viewer"
        organization_membership.role = "viewer"
        await db.commit()
        viewer = await task_routes.task_capabilities(task.id, actor, db)
        assert viewer.allowed_commands == []
        assert viewer.allowed_transitions == []
        assert not viewer.can_answer_questions and not viewer.can_edit_artifacts

        membership.role = "operator"
        for status, expected in (
            ("PAUSED", {"resume", "cancel"}),
            ("FAILED", {"cancel", "retry"}),
            ("SUCCEEDED", set()),
        ):
            task.run_status = status
            await db.commit()
            capabilities = await task_routes.task_capabilities(task.id, actor, db)
            assert {item.value for item in capabilities.allowed_commands} == expected

        with pytest.raises(HTTPException) as missing:
            await task_routes.task_capabilities(uuid4(), actor, db)
        assert missing.value.status_code == 404


@pytest.mark.anyio
async def test_project_event_stream_replays_heartbeats_and_recovers_expired_cursors() -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        assert actor is not None
        project_id = data["project_id"]
        await task_routes.create_project_task(
            project_id,
            TaskCreate(title="Stream fixture", description="Durable stream test."),
            Response(status_code=201),
            "stream-fixture",
            actor,
            db,
        )
        sequence = await db.scalar(
            select(func.max(task_routes.DomainEvent.project_sequence)).where(
                task_routes.DomainEvent.project_id == project_id
            )
        )
        assert sequence is not None

        polls = 0

        async def disconnect_after_one_poll() -> bool:
            nonlocal polls
            polls += 1
            return polls > 1

        request = SimpleNamespace(is_disconnected=disconnect_after_one_poll)
        response = await task_routes.stream_project_events(project_id, request, 0, actor, db)
        assert response.media_type == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-store"
        replay_chunks = [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in response.body_iterator
        ]
        replay = "".join(replay_chunks)
        assert replay.startswith("retry: 3000")
        assert "id: " in replay and "data: " in replay
        replay_ids = [
            int(line.split(":", 1)[1])
            for line in replay.splitlines()
            if line.startswith("id:")
        ]
        assert replay_ids
        assert all(left < right for left, right in zip(replay_ids, replay_ids[1:]))

        polls = 0
        heartbeat_request = SimpleNamespace(is_disconnected=disconnect_after_one_poll)
        heartbeat_response = await task_routes.stream_project_events(
            project_id, heartbeat_request, sequence, actor, db
        )
        heartbeat_chunks = [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in heartbeat_response.body_iterator
        ]
        heartbeat = "".join(heartbeat_chunks)
        assert ": heartbeat" in heartbeat

        await db.execute(
            update(task_routes.DomainEvent)
            .where(task_routes.DomainEvent.project_id == project_id)
            .values(retention_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
        with pytest.raises(HTTPException) as expired:
            await task_routes.stream_project_events(
                project_id, SimpleNamespace(is_disconnected=disconnect_after_one_poll), 0, actor, db
            )
        assert expired.value.status_code == 410
        assert expired.value.detail["code"] == "event_cursor_expired"


@pytest.mark.anyio
async def test_stream_disconnect_and_reconnect_do_not_create_or_cancel_runs() -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        assert actor is not None
        project_id = data["project_id"]
        await task_routes.create_project_task(
            project_id,
            TaskCreate(title="Reconnect fixture", description="Reader lifecycle test."),
            Response(status_code=201),
            "reconnect-stream-fixture",
            actor,
            db,
        )
        run_count = await db.scalar(
            select(func.count()).select_from(task_routes.Run).where(
                task_routes.Run.task_id == data["task_id"]
            )
        )
        polls = 0

        async def disconnect_after_replay() -> bool:
            nonlocal polls
            polls += 1
            return polls > 1

        first = await task_routes.stream_project_events(
            project_id,
            SimpleNamespace(is_disconnected=disconnect_after_replay),
            0,
            actor,
            db,
        )
        first_chunks = [chunk async for chunk in first.body_iterator]
        first_replay_ids = [
            int(line.split(":", 1)[1])
            for chunk in first_chunks
            for line in (chunk.decode() if isinstance(chunk, bytes) else chunk).splitlines()
            if line.startswith("id:")
        ]
        assert first_replay_ids
        assert all(left < right for left, right in zip(first_replay_ids, first_replay_ids[1:]))
        resume_cursor = first_replay_ids[-1]

        polls = 0
        second = await task_routes.stream_project_events(
            project_id,
            SimpleNamespace(is_disconnected=disconnect_after_replay),
            resume_cursor,
            actor,
            db,
        )
        second_chunks = [chunk async for chunk in second.body_iterator]
        assert any("heartbeat" in (chunk.decode() if isinstance(chunk, bytes) else chunk) for chunk in second_chunks)

        remaining_runs = await db.scalar(
            select(func.count()).select_from(task_routes.Run).where(
                task_routes.Run.task_id == data["task_id"]
            )
        )
        run = await db.get(task_routes.Run, data["run_id"])
        assert remaining_runs == run_count == 1
        assert run is not None and run.status == "RUNNING"


@pytest.mark.anyio
async def test_project_event_stream_reports_history_expiry_during_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        assert actor is not None
        project_id = data["project_id"]
        for index in range(3):
            await task_routes.create_project_task(
                project_id,
                TaskCreate(title=f"Tail fixture {index}", description="Expiry boundary test."),
                Response(status_code=201),
                f"tail-fixture-{index}",
                actor,
                db,
            )
        latest = await db.scalar(
            select(func.max(task_routes.DomainEvent.project_sequence)).where(
                task_routes.DomainEvent.project_id == project_id
            )
        )
        assert latest is not None and latest >= 3
        monkeypatch.setattr(settings, "event_replay_limit", 1)
        polls = 0

        async def expire_unread_history() -> bool:
            nonlocal polls
            polls += 1
            if polls == 2:
                await db.execute(
                    update(task_routes.DomainEvent)
                    .where(
                        task_routes.DomainEvent.project_id == project_id,
                        task_routes.DomainEvent.project_sequence < latest,
                    )
                    .values(retention_until=datetime.now(UTC) - timedelta(seconds=1))
                )
                await db.commit()
            return False

        response = await task_routes.stream_project_events(
            project_id, SimpleNamespace(is_disconnected=expire_unread_history), 0, actor, db
        )
        chunks = [
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in response.body_iterator
        ]
        assert any('"name":"harnessa.cursor_expired"' in chunk for chunk in chunks)


@pytest.mark.anyio
async def test_project_event_stream_rechecks_membership_and_emits_revocation() -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        assert actor is not None
        project_id = data["project_id"]
        polls = 0

        async def revoke_after_replay() -> bool:
            nonlocal polls
            polls += 1
            if polls == 2:
                await db.execute(
                    delete(Membership).where(
                        Membership.user_id == actor.id,
                        Membership.organization_id == data["organization_id"],
                    )
                )
                await db.commit()
            return False

        await task_routes.create_project_task(
            project_id,
            TaskCreate(title="Revocation stream fixture", description="Reauthorize test."),
            Response(status_code=201),
            "revocation-stream-fixture",
            actor,
            db,
        )
        response = await task_routes.stream_project_events(
            project_id, SimpleNamespace(is_disconnected=revoke_after_replay), 0, actor, db
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        assert any('"name":"harnessa.access_revoked"' in chunk for chunk in chunks)


@pytest.mark.anyio
async def test_task_routes_cover_cursors_history_events_and_operator_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await gateway_fixture()
    async with SessionLocal() as db:
        actor = await db.get(task_routes.User, data["user_id"])
        assert actor is not None
        project_id = data["project_id"]
        response = Response(status_code=201)
        created = await task_routes.create_project_task(
            project_id,
            TaskCreate(
                title="Route-created task",
                description="Exercise the task API with a disposable database.",
                priority=3,
            ),
            response,
            "route-task-create",
            actor,
            db,
        )
        assert response.status_code == 201
        assert created.priority == 3

        replay_response = Response()
        replayed = await task_routes.create_project_task(
            project_id,
            TaskCreate(
                title="Route-created task",
                description="Exercise the task API with a disposable database.",
                priority=3,
            ),
            replay_response,
            "route-task-create",
            actor,
            db,
        )
        assert replayed.id == created.id
        assert replay_response.status_code == 200
        assert replay_response.headers["Idempotency-Replayed"] == "true"

        listed = await task_routes.list_project_tasks(project_id, 1, None, actor, db)
        assert len(listed) == 1
        assert listed[0].id == created.id
        older = await task_routes.list_project_tasks(project_id, 10, created.id, actor, db)
        assert [task.id for task in older] == [data["task_id"]]
        with pytest.raises(HTTPException) as cursor_missing:
            await task_routes.list_project_tasks(project_id, 10, uuid4(), actor, db)
        assert cursor_missing.value.status_code == 400

        fetched = await task_routes.get_task(created.id, actor, db)
        assert fetched.id == created.id
        with pytest.raises(HTTPException) as task_missing:
            await task_routes.get_task(uuid4(), actor, db)
        assert task_missing.value.status_code == 404

        existing_task = await task_routes.get_task(data["task_id"], actor, db)
        runs = await task_routes.list_runs(data["task_id"], actor, db)
        transitions = await task_routes.list_transitions(created.id, actor, db)
        assert existing_task.id == data["task_id"]
        assert [run.id for run in runs] == [data["run_id"]]
        assert transitions
        with pytest.raises(HTTPException) as missing_run_task:
            await task_routes.list_runs(uuid4(), actor, db)
        assert missing_run_task.value.status_code == 404
        with pytest.raises(HTTPException) as missing_transition_task:
            await task_routes.list_transitions(uuid4(), actor, db)
        assert missing_transition_task.value.status_code == 404

        monkeypatch.setattr(settings, "event_replay_limit", 20)
        page = await task_routes.replay_events(project_id, 0, 10, actor, db)
        assert page.items
        assert page.replay_limit == 20
        with pytest.raises(HTTPException) as oversized:
            await task_routes.replay_events(project_id, 0, 21, actor, db)
        assert oversized.value.status_code == 422

        assert await task_routes.list_dead_letters(project_id, actor, db) == []

        async def rebuild(redis):
            assert redis == "fixture-redis"
            return 4

        monkeypatch.setattr(task_routes, "rebuild_dispatch_stream", rebuild)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis="fixture-redis")))
        rebuilt = await task_routes.rebuild_dispatch(request, actor, db)
        assert rebuilt.rebuilt == 4

        assert task_routes._if_match(' W/"7" ') == 7
        with pytest.raises(HTTPException) as malformed_version:
            task_routes._if_match("not-a-version")
        assert malformed_version.value.status_code == 400
        with pytest.raises(HTTPException) as nonpositive_version:
            task_routes._if_match('"0"')
        assert nonpositive_version.value.status_code == 400
