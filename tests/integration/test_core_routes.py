from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from harnessa.api import routes
from harnessa.config import settings
from harnessa.contracts import ProjectCreate, RepositoryCreate
from harnessa.db.models import Membership, Organization, User
from harnessa.db.session import SessionLocal
from sqlalchemy import select
from starlette.requests import Request

pytestmark = pytest.mark.integration


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth/bootstrap",
            "raw_path": b"/auth/bootstrap",
            "query_string": b"",
            "headers": [(b"x-bootstrap-token", token.encode())],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        }
    )


@pytest.mark.anyio
async def test_bootstrap_is_token_gated_and_creates_only_one_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bootstrap_token", "fixture-bootstrap-secret")
    body = routes.BootstrapUser(
        email="Initial.Admin@example.com",
        password="long-fixture-password",
        organization_name="Fixture Bootstrap",
    )
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as missing_token:
            await routes.bootstrap(body, _request("wrong-token"), db)
        assert missing_token.value.status_code == 404

        created = await routes.bootstrap(body, _request("fixture-bootstrap-secret"), db)
        assert created["email"] == "initial.admin@example.com"
        assert created["id"]
        assert created["organization_id"]
        pools = (await db.scalars(select(routes.ModelPool))).all()
        assert {pool.kind for pool in pools} == {"interactive", "probe"}

        with pytest.raises(HTTPException) as already_bootstrapped:
            await routes.bootstrap(body, _request("fixture-bootstrap-secret"), db)
        assert already_bootstrapped.value.status_code == 409


@pytest.mark.anyio
async def test_auth_project_and_repository_routes_enforce_idempotency_and_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex[:10]
    async with SessionLocal() as db:
        organization = Organization(name=f"Route Test {suffix}")
        user = User(
            email=f"routes-{suffix}@example.test",
            hashed_password="not-used-for-direct-route-test",
            is_active=True,
            is_superuser=True,
            is_verified=True,
        )
        unlinked_user = User(
            email=f"unlinked-{suffix}@example.test",
            hashed_password="not-used-for-direct-route-test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add_all([organization, user, unlinked_user])
        await db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role="administrator"))
        await db.commit()

        identity = await routes.identity(user, db)
        assert identity.email == user.email
        assert identity.organization_id == organization.id
        assert identity.role.value == "administrator"
        assert await routes.projects(unlinked_user, db) == []

        project_body = ProjectCreate(name="Route Project", slug=f"route-{suffix}")
        project = await routes.create_project(project_body, f"project-{suffix}", user, db)
        assert project.slug == f"route-{suffix}"
        project_id = project.id
        replay = await routes.create_project(project_body, f"project-{suffix}", user, db)
        assert replay.status_code == 201
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.body
        assert len(await routes.projects(user, db)) == 1

        with pytest.raises(HTTPException) as idempotency_conflict:
            await routes.create_project(
                ProjectCreate(name="Changed", slug=f"changed-{suffix}"),
                f"project-{suffix}",
                user,
                db,
            )
        assert idempotency_conflict.value.status_code == 409
        with pytest.raises(HTTPException) as duplicate_slug:
            await routes.create_project(
                ProjectCreate(name="Duplicate slug", slug=f"route-{suffix}"),
                f"project-duplicate-{suffix}",
                user,
                db,
            )
        assert duplicate_slug.value.status_code == 409
        await db.refresh(user)
        with pytest.raises(HTTPException) as invalid_key:
            await routes.create_project(project_body, "", user, db)
        assert invalid_key.value.status_code == 400

        monkeypatch.setattr(settings, "repository_targets", "fixture/repository")
        repository_body = RepositoryCreate(
            target_id="fixture/repository",
            display_name="Fixture Repository",
            default_branch="main",
        )
        repository = await routes.add_repository(
            project_id, repository_body, f"repo-{suffix}", user, db
        )
        assert repository.target_id == "fixture/repository"
        assert len(await routes.repositories(project_id, user, db)) == 1
        repo_replay = await routes.add_repository(
            project_id, repository_body, f"repo-{suffix}", user, db
        )
        assert repo_replay.status_code == 201
        assert repo_replay.headers["Idempotency-Replayed"] == "true"

        with pytest.raises(HTTPException) as repo_key_conflict:
            await routes.add_repository(
                project_id,
                RepositoryCreate(
                    target_id="fixture/repository",
                    display_name="Different Name",
                    default_branch="main",
                ),
                f"repo-{suffix}",
                user,
                db,
            )
        assert repo_key_conflict.value.status_code == 409
        with pytest.raises(HTTPException) as target_not_allowed:
            await routes.add_repository(
                project_id,
                RepositoryCreate(
                    target_id="unconfigured/repository",
                    display_name="Unconfigured",
                    default_branch="main",
                ),
                f"repo-invalid-{suffix}",
                user,
                db,
            )
        assert target_not_allowed.value.status_code == 422
        with pytest.raises(HTTPException) as repository_duplicate:
            await routes.add_repository(
                project_id,
                repository_body,
                f"repo-duplicate-{suffix}",
                user,
                db,
            )
        assert repository_duplicate.value.status_code == 409


@pytest.mark.anyio
async def test_cookie_helpers_set_and_clear_only_authentication_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cookie_secure", False)
    response = Response()
    csrf = await routes.csrf(response)
    assert len(csrf["csrf_token"]) >= 32
    cookies = response.headers.getlist("set-cookie")
    assert any("harnessa_csrf=" in item and "HttpOnly" not in item for item in cookies)
    assert all("Path=/" in item for item in cookies)

    await routes.logout(response)
    deleted = response.headers.getlist("set-cookie")
    assert any("harnessa_auth=" in item and "Max-Age=0" in item for item in deleted)
    assert any("harnessa_csrf=" in item and "Max-Age=0" in item for item in deleted)
