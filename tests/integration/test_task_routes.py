from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from harnessa.api import task_routes
from harnessa.config import settings
from harnessa.contracts import TaskCreate
from harnessa.db.session import SessionLocal

from tests.integration.test_model_gateway import gateway_fixture

pytestmark = pytest.mark.integration


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
