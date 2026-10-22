"""Versioned repository connector and idempotent metadata-only index synchronization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from harnessa.adapters.specification_workspace import (
    SpecificationWorkspaceError,
    SpecificationWorkspaceManager,
)
from harnessa.config import settings
from harnessa.db.models import Project, Repository, User
from harnessa.db.retrieval import RetrievalChunk, RetrievalSource, RetrievalSync
from harnessa.domain.retrieval import RetrievalSourceRecord
from harnessa.services.authorization import require_membership
from harnessa.services.retrieval_governance import (
    IndexedChunk,
    SourceDocument,
    eligible_path,
    split_chunks,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

CONNECTOR_VERSIONS = {
    "repository": "git-tree-v2",
    "openspec": "openspec-git-v1",
    "approved_docs": "approved-docs-v1",
    "verified_runs": "verified-runs-v1",
    "approved_skills": "approved-skills-v1",
}


def repository_connector_version(approved_skill_paths: tuple[str, ...]) -> str:
    policy_hash = hashlib.sha256("\0".join(sorted(approved_skill_paths)).encode()).hexdigest()[:32]
    return f"git-tree-v2-{policy_hash}"


def approved_skill_paths_for_project(project_id: UUID) -> tuple[str, ...]:
    try:
        approved_by_project = json.loads(settings.retrieval_approved_skill_paths_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "Approved skill path configuration is invalid") from exc
    if not isinstance(approved_by_project, dict):
        raise HTTPException(503, "Approved skill paths must map project IDs to path lists")
    approved_paths = approved_by_project.get(str(project_id), [])
    if not isinstance(approved_paths, list) or any(not isinstance(path, str) for path in approved_paths):
        raise HTTPException(503, "Approved skill path configuration is invalid")
    if any(
        not path.startswith(".agents/skills/") or not path.endswith("/SKILL.md")
        for path in approved_paths
    ):
        raise HTTPException(503, "Only explicitly named SKILL.md paths under .agents/skills may be indexed")
    return tuple(approved_paths)


def source_type_for(path: str) -> str:
    if path.startswith("openspec/"):
        return "openspec"
    if path.startswith(("docs/", "planning/")):
        return "approved_docs"
    if path == "skills-lock.json" or path.startswith(("skills/", ".agents/skills/")):
        return "approved_skills"
    return "repository"


async def sync_repository_index(
    db: AsyncSession,
    *,
    project_id: UUID,
    repository_id: UUID,
    actor: User,
    manager: SpecificationWorkspaceManager,
    dry_run: bool,
) -> dict[str, object]:
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Repository indexing requires project operator access")
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
        )
    )
    if repository is None:
        raise HTTPException(404, "Repository is not connected to this project")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    approved_paths = approved_skill_paths_for_project(project_id)
    connector_version = repository_connector_version(approved_paths)
    previous_sync = await db.scalar(
        select(RetrievalSync)
        .where(
            RetrievalSync.repository_id == repository_id,
            RetrievalSync.state == "succeeded",
            RetrievalSync.revision.is_not(None),
        )
        .order_by(RetrievalSync.finished_at.desc())
        .limit(1)
    )
    complete_snapshot = await db.scalar(
        select(RetrievalSync)
        .where(
            RetrievalSync.repository_id == repository_id,
            RetrievalSync.state == "succeeded",
            RetrievalSync.incremental.is_(False),
            RetrievalSync.snapshot_complete.is_(True),
        )
        .order_by(RetrievalSync.finished_at.desc())
        .limit(1)
    )
    delta_snapshot = getattr(manager, "snapshot_changes", None)
    use_delta = (
        not dry_run
        and complete_snapshot is not None
        and previous_sync is not None
        and previous_sync.connector_version == connector_version
        and callable(delta_snapshot)
    )
    try:
        await require_membership(db, actor, project_id)
        deleted_paths: tuple[str, ...] = ()
        incremental = False
        if use_delta and previous_sync is not None and callable(delta_snapshot):
            current_revision, files, deleted_paths, snapshot_omitted, incremental = await delta_snapshot(
                repository, previous_sync.revision, tuple(approved_paths)
            )
        else:
            current_revision, files, snapshot_omitted = await manager.snapshot(
                repository, tuple(approved_paths)
            )
    except (OSError, SpecificationWorkspaceError) as exc:
        failed_at = datetime.now(UTC)
        db.add(
            RetrievalSync(
                organization_id=project.organization_id,
                project_id=project_id,
                repository_id=repository_id,
                connector_id="git-repository-v1",
                connector_version=CONNECTOR_VERSIONS["repository"],
                revision=None,
                state="failed",
                error_code=type(exc).__name__,
                started_at=failed_at,
                finished_at=failed_at,
                considered_count=0,
                indexed_count=0,
                omitted_counts={"connector_unavailable": 1},
            )
        )
        await db.commit()
        raise HTTPException(503, "Repository connector is unavailable") from exc

    omitted: dict[str, int] = dict(snapshot_omitted)
    snapshot_complete = not snapshot_omitted
    considered_count = len(files) + len(deleted_paths) + sum(snapshot_omitted.values())
    candidates: list[tuple[SourceDocument, tuple[IndexedChunk, ...]]] = []
    for item in files:
        source_type = source_type_for(item.path)
        approved_skill = item.path in approved_paths and source_type == "approved_skills"
        if not eligible_path(item.path, approved_external=approved_skill):
            omitted["path_policy"] = omitted.get("path_policy", 0) + 1
            continue
        source_key = hashlib.sha256(f"{repository_id}:{item.path}".encode()).hexdigest()
        document = SourceDocument(
            source_id=source_key,
            source_type=source_type,
            organization_id=str(project.organization_id),
            project_id=str(project_id),
            repository_id=str(repository_id),
            source_uri=f"repo://{repository_id}/{item.path}",
            revision=current_revision,
            path=item.path,
            content=item.content,
            license="connected-repository-policy",
            retention="until-source-disconnect",
            sensitivity="internal",
            access_policy="current-project-repository-membership",
            approved=True,
        )
        chunk_records = split_chunks(document)
        if not chunk_records:
            omitted["secret_or_empty_or_policy"] = omitted.get("secret_or_empty_or_policy", 0) + 1
            continue
        candidates.append((document, chunk_records))

    if dry_run:
        return {
            "dry_run": True,
            "incremental": False,
            "snapshot_complete": snapshot_complete,
            "revision": current_revision,
            "considered": considered_count,
            "source_count": len(candidates),
            "chunk_count": sum(len(chunks) for _, chunks in candidates),
            "omitted": omitted,
            "limits": {"files": 2500, "total_bytes": 32_000_000, "file_bytes": 512_000, "chunk_bytes": 4000},
        }

    await require_membership(db, actor, project_id)
    now = datetime.now(UTC)
    sync = RetrievalSync(
        organization_id=project.organization_id,
        project_id=project_id,
        repository_id=repository_id,
        connector_id="git-repository-v1",
        connector_version=connector_version,
        revision=current_revision,
        state="running",
        started_at=now,
        considered_count=considered_count,
        indexed_count=len(candidates),
        incremental=incremental,
        snapshot_complete=snapshot_complete,
        omitted_counts=omitted,
    )
    db.add(sync)
    existing_sources = list(
        await db.scalars(
            select(RetrievalSource).where(
                RetrievalSource.project_id == project_id,
                RetrievalSource.repository_id == repository_id,
                RetrievalSource.state == "current",
            )
        )
    )
    current_keys = {document.source_id for document, _ in candidates}
    changed_keys = {
        hashlib.sha256(f"{repository_id}:{path}".encode()).hexdigest()
        for path in deleted_paths
    }
    changed_keys.update(
        hashlib.sha256(f"{repository_id}:{item.path}".encode()).hexdigest() for item in files
    )
    if not incremental:
        changed_keys.update(source.source_key for source in existing_sources)
    for source in existing_sources:
        if source.source_key not in changed_keys:
            if incremental or source.source_key in current_keys or snapshot_complete:
                continue
            source.state = "stale"
        elif source.source_key in current_keys:
            if source.revision == current_revision:
                continue
            source.state = "stale"
        else:
            source.state = "tombstoned" if incremental or snapshot_complete else "stale"
        if source.state not in {"stale", "tombstoned"}:
            continue
        chunks = await db.scalars(
            select(RetrievalChunk).where(RetrievalChunk.source_record_id == source.id)
        )
        for stored_chunk in chunks:
            stored_chunk.state = "tombstoned"

    for document, candidate_chunks in candidates:
        existing = await db.scalar(
            select(RetrievalSource).where(
                RetrievalSource.project_id == project_id,
                RetrievalSource.source_key == document.source_id,
                RetrievalSource.revision == current_revision,
            )
        )
        if existing is None:
            existing = RetrievalSource(
                organization_id=project.organization_id,
                project_id=project_id,
                repository_id=repository_id,
                source_key=document.source_id,
                source_type=document.source_type,
                source_uri=document.source_uri,
                revision=current_revision,
                content_hash=hashlib.sha256(document.content.encode()).hexdigest(),
                license=document.license,
                retention=document.retention,
                sensitivity=document.sensitivity,
                access_policy=document.access_policy,
                connector_version=CONNECTOR_VERSIONS.get(document.source_type, "git-tree-v2"),
                state="current",
                source_created_at=None,
                synced_at=now,
            )
            db.add(existing)
            await db.flush()
            for chunk in candidate_chunks:
                db.add(
                    RetrievalChunk(
                        organization_id=project.organization_id,
                        project_id=project_id,
                        repository_id=repository_id,
                        source_record_id=existing.id,
                        source_type=document.source_type,
                        chunk_key=chunk.chunk_id,
                        content_hash=chunk.content_hash,
                        path=chunk.path,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        state="current",
                    )
                )
        else:
            existing.state = "current"
            existing.synced_at = now
    sync.state = "succeeded"
    sync.finished_at = datetime.now(UTC)
    sync.indexed_count = len(candidates)
    await db.commit()
    return {
        "dry_run": False,
        "incremental": incremental,
        "snapshot_complete": snapshot_complete,
        "revision": current_revision,
        "considered": considered_count,
        "source_count": len(candidates),
        "chunk_count": sum(len(chunk_set) for _, chunk_set in candidates),
        "omitted": omitted,
    }


def source_record_to_contract(record: RetrievalSource) -> RetrievalSourceRecord:
    return RetrievalSourceRecord(
        source_id=record.source_key,
        source_type=record.source_type,
        source_uri=record.source_uri,
        organization_id=str(record.organization_id),
        project_id=str(record.project_id),
        repository_id=str(record.repository_id) if record.repository_id else None,
        revision=record.revision,
        content_hash=record.content_hash,
        created_at=record.synced_at,
        license=record.license,
        retention=record.retention,
        sensitivity=record.sensitivity,
        access_policy=record.access_policy,
        stale=record.state == "stale",
        tombstoned=record.state == "tombstoned",
    )
