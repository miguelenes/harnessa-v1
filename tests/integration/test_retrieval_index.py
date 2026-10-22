from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.api import retrieval_routes
from harnessa.config import settings
from harnessa.contracts import MCPToolInvokeRequest
from harnessa.db.models import (
    Membership,
    Organization,
    Project,
    ProjectMembership,
    Repository,
    User,
)
from harnessa.db.retrieval import (
    MCPProjectGrant,
    MCPToolAudit,
    RetrievalChunk,
    RetrievalSource,
    RetrievalSync,
)
from harnessa.db.session import SessionLocal
from harnessa.services.retrieval_governance import MCPTool
from harnessa.services.retrieval_index import sync_repository_index
from harnessa.services.specifications import search_repository
from pwdlib import PasswordHash
from sqlalchemy import delete, select, update

pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


async def _source_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "source"
    root.mkdir()
    _git(root.parent, "init", "--quiet", "--initial-branch=main", str(root))
    (root / "openspec/specs/example").mkdir(parents=True)
    (root / "openspec/changes/example-change").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".agents/skills/approved").mkdir(parents=True)
    (root / ".evals").mkdir()
    (root / "openspec/specs/example/spec.md").write_text("# Example\n\nBounded retrieval policy.\n")
    (root / "openspec/changes/example-change/proposal.md").write_text(
        "# Change history\n\nUnique changelog artifact for retrieval citations.\n"
    )
    (root / "docs/guide.md").write_text("Approved project operations.\n")
    (root / ".agents/skills/approved/SKILL.md").write_text(
        "# Approved skill\n\nUnique skill instruction for retrieval citations.\n"
    )
    (root / "docs/private.md").write_text("OPENAI_API_KEY=sk-123456789012345678901234567890\n")
    (root / ".evals/answer.md").write_text("hidden grader answer\n")
    _git(root, "add", "--all")
    await asyncio.to_thread(_commit, root, "initial")
    monkeypatch.setattr(settings, "openspec_repository_sources_json", json.dumps({"fixture/source": str(root)}))
    async with SessionLocal() as db:
        organization = Organization(name="Retrieval fixture")
        user = User(
            email=f"retrieval-{uuid4()}@example.test",
            hashed_password=PasswordHash.recommended().hash("fixture-password-long-enough"),
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add_all([organization, user])
        await db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role="administrator"))
        project = Project(organization_id=organization.id, name="Retrieval", slug=f"retrieval-{uuid4().hex[:8]}")
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=user.id, role="administrator"))
        repository = Repository(project_id=project.id, target_id="fixture/source", display_name="Fixture", default_branch="main")
        db.add(repository)
        await db.commit()
        await db.refresh(user)
        await db.refresh(repository)
        return root, project.id, repository.id, user, SpecificationWorkspaceManager(root=tmp_path / "workspaces")


async def test_repository_sync_preview_duplicate_update_and_deletion_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_id, repository_id, actor, manager = await _source_fixture(tmp_path, monkeypatch)
    async with SessionLocal() as db:
        preview = await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=True
        )
        assert preview["dry_run"] is True
        assert preview["snapshot_complete"] is True
        assert preview["considered"] == 4
        first = await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=False
        )
        assert first["chunk_count"] >= 2
        assert first["snapshot_complete"] is True
        second = await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=False
        )
        assert second["revision"] == first["revision"]
        records = list(await db.scalars(select(RetrievalSource).where(RetrievalSource.project_id == project_id)))
        keys = [source.source_key for source in records]
        assert len(keys) == len(set(keys))
        assert all("private.md" not in source.source_uri for source in records)
        assert all(".evals" not in source.source_uri for source in records)
        assert len(list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.state == "current")))) >= 2

        unchanged_path = "openspec/specs/example/spec.md"
        unchanged_before = await db.scalar(
            select(RetrievalSource).where(RetrievalSource.source_uri.endswith(unchanged_path))
        )
        assert unchanged_before is not None and unchanged_before.state == "current"
        old_guide = await db.scalar(
            select(RetrievalSource).where(RetrievalSource.source_uri.endswith("docs/guide.md"))
        )
        assert old_guide is not None
        old_guide_revision = old_guide.revision

        (root / "docs/guide.md").write_text("Updated approved project operations.\n")
        _git(root, "add", "docs/guide.md")
        await asyncio.to_thread(_commit, root, "update guide")
        updated = await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=False
        )
        assert updated["incremental"] is True
        assert updated["source_count"] == 1
        assert updated["chunk_count"] == 1
        guide_revisions = list(
            await db.scalars(
                select(RetrievalSource).where(RetrievalSource.source_uri.endswith("docs/guide.md"))
            )
        )
        assert len(guide_revisions) == 2
        assert next(source for source in guide_revisions if source.revision == old_guide_revision).state == "stale"
        current_guide = next(source for source in guide_revisions if source.state == "current")
        assert current_guide.revision == updated["revision"]
        unchanged_after = await db.scalar(
            select(RetrievalSource).where(RetrievalSource.source_uri.endswith(unchanged_path))
        )
        assert unchanged_after is not None and unchanged_after.state == "current"
        assert unchanged_after.revision == unchanged_before.revision

        repository = await db.get(Repository, repository_id)
        assert repository is not None
        fallback = await manager.snapshot_changes(repository, "0" * 40)
        assert fallback[0] == updated["revision"]
        assert fallback[4] is False
        assert fallback[1]

        (root / "docs/guide.md").write_text("OPENAI_API_KEY=sk-123456789012345678901234567890\n")
        _git(root, "add", "docs/guide.md")
        await asyncio.to_thread(_commit, root, "replace guide with secret-like content")
        filtered_update = await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=False
        )
        assert filtered_update["incremental"] is True
        assert filtered_update["source_count"] == 0
        assert await db.scalar(
            select(RetrievalSource.id).where(
                RetrievalSource.source_uri.endswith("docs/guide.md"),
                RetrievalSource.state == "current",
            )
        ) is None

        (root / "docs/guide.md").unlink()
        _git(root, "add", "--all")
        await asyncio.to_thread(_commit, root, "delete source")
        await sync_repository_index(
            db, project_id=project_id, repository_id=repository_id, actor=actor, manager=manager, dry_run=False
        )
        assert await db.scalar(
            select(RetrievalChunk.id).where(
                RetrievalChunk.path == "docs/guide.md", RetrievalChunk.state == "tombstoned"
            )
        ) is not None
        assert await db.scalar(
            select(RetrievalSource.id).where(
                RetrievalSource.source_uri.endswith("/docs/guide.md"), RetrievalSource.state == "tombstoned"
            )
        ) is not None


async def test_incomplete_bounded_snapshot_marks_unseen_sources_stale_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project_id, repository_id, actor, manager = await _source_fixture(tmp_path, monkeypatch)
    async with SessionLocal() as db:
        await sync_repository_index(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            manager=manager,
            dry_run=False,
        )

        class BoundedSnapshot:
            async def snapshot(self, repository: Repository, approved_skill_paths: tuple[str, ...] = ()):
                revision, files, omitted = await manager.snapshot(repository, approved_skill_paths)
                return (
                    revision,
                    tuple(item for item in files if item.path != "docs/guide.md"),
                    {**omitted, "file_limit": 1},
                )

        result = await sync_repository_index(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            manager=BoundedSnapshot(),  # type: ignore[arg-type]
            dry_run=False,
        )
        assert result["snapshot_complete"] is False
        guide = await db.scalar(
            select(RetrievalSource).where(RetrievalSource.source_uri.endswith("/docs/guide.md"))
        )
        assert guide is not None
        assert guide.state == "stale"
        assert await db.scalar(
            select(RetrievalSource.id).where(
                RetrievalSource.source_uri.endswith("/docs/guide.md"),
                RetrievalSource.state == "tombstoned",
            )
        ) is None


async def test_connector_outage_is_recorded_without_losing_current_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project_id, repository_id, actor, _manager = await _source_fixture(tmp_path, monkeypatch)

    class OfflineConnector:
        async def snapshot(
            self, _: Repository, approved_skill_paths: tuple[str, ...] = ()
        ) -> tuple[str, tuple[object, ...], dict[str, int]]:
            raise SpecificationWorkspaceError("fixture outage")

    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            await sync_repository_index(
                db,
                project_id=project_id,
                repository_id=repository_id,
                actor=actor,
                manager=OfflineConnector(),  # type: ignore[arg-type]
                dry_run=False,
            )
        assert error.value.status_code == 503
        sync = await db.scalar(
            select(RetrievalSync).where(RetrievalSync.repository_id == repository_id)
        )
        assert sync is not None
        assert sync.state == "failed"
        assert sync.error_code == "SpecificationWorkspaceError"


async def test_repository_search_rechecks_membership_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project_id, repository_id, actor, _manager = await _source_fixture(tmp_path, monkeypatch)
    async with SessionLocal() as db:
        result = await search_repository(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            query="Bounded retrieval policy",
            limit=5,
        )
        assert result.citations
        secret_search = await search_repository(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            query="OPENAI_API_KEY sk-123456789012345678901234567890",
            limit=5,
        )
        hidden_eval_search = await search_repository(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            query="hidden grader answer",
            limit=5,
        )
        assert secret_search.citations == ()
        assert hidden_eval_search.citations == ()
        project = await db.get(Project, project_id)
        assert project is not None
        await db.execute(
            update(Membership)
            .where(
                Membership.organization_id == project.organization_id,
                Membership.user_id == actor.id,
            )
            .values(role="operator")
        )
        await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await search_repository(
                db,
                project_id=project_id,
                repository_id=repository_id,
                actor=actor,
                query="Bounded retrieval policy",
                limit=5,
            )
        assert error.value.status_code == 403


async def test_search_cites_synced_openspec_history_and_project_approved_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project_id, repository_id, actor, manager = await _source_fixture(tmp_path, monkeypatch)
    skill_path = ".agents/skills/approved/SKILL.md"
    monkeypatch.setattr(
        settings,
        "retrieval_approved_skill_paths_json",
        json.dumps({str(project_id): [skill_path]}),
    )
    async with SessionLocal() as db:
        indexed = await sync_repository_index(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            manager=manager,
            dry_run=False,
        )
        assert indexed["source_count"] >= 4
        history = await search_repository(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            query="unique changelog artifact",
            limit=5,
        )
        approved_skill = await search_repository(
            db,
            project_id=project_id,
            repository_id=repository_id,
            actor=actor,
            query="unique skill instruction",
            limit=5,
        )
        assert any(item.path == "openspec/changes/example-change/proposal.md" for item in history.citations)
        assert any(item.path == skill_path for item in approved_skill.citations)


async def test_mcp_result_is_suppressed_when_project_grant_is_revoked_during_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project_id, _, actor, _manager = await _source_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "mcp_servers_json", json.dumps({"fixture": "https://fixture.test"}))
    monkeypatch.setattr(settings, "mcp_read_only_tools_json", json.dumps({"fixture": ["lookup"]}))
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        grant = MCPProjectGrant(
            organization_id=project.organization_id,
            project_id=project_id,
            created_by=actor.id,
            server_id="fixture",
            allowed_tools=["lookup"],
            read_only_tools=["lookup"],
            protocol_version="2025-11-25",
            secret_reference=None,
            enabled=True,
        )
        db.add(grant)
        await db.commit()
        await db.refresh(grant)
        grant_id = grant.id

        class RevokingMCPClient:
            def __init__(self, _: dict[str, str], **__: object) -> None:
                pass

            async def discover(self, *_: object) -> tuple[MCPTool, ...]:
                return (MCPTool("lookup", "fixture", {"type": "object", "properties": {}}),)

            async def invoke(self, *_: object) -> bytes:
                async with SessionLocal() as concurrent_db:
                    current = await concurrent_db.get(MCPProjectGrant, grant_id)
                    assert current is not None
                    current.enabled = False
                    current.revoked_at = datetime.now(UTC)
                    await concurrent_db.commit()
                return b"private remote content"

        monkeypatch.setattr(
            "harnessa.api.retrieval_routes.StreamableHTTPMCPClient", RevokingMCPClient
        )
        with pytest.raises(HTTPException) as error:
            await retrieval_routes.invoke_mcp(
                project_id,
                "fixture",
                "lookup",
                MCPToolInvokeRequest(arguments={}),
                actor=actor,
                db=db,
            )
        assert error.value.status_code == 403
        audit = await db.scalar(
            select(MCPToolAudit).where(
                MCPToolAudit.project_id == project_id,
                MCPToolAudit.tool_name == "lookup",
            )
        )
        assert audit is not None
        assert audit.outcome == "failed"
        assert audit.response_hash is not None
