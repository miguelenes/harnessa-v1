"""Trusted verifier write port for immutable, run-scoped result evidence.

This module intentionally has no HTTP route: only a server-side verifier adapter
may call it. A successful scheduler run never creates verification evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from harnessa.adapters.specification_workspace import SpecificationWorkspaceManager
from harnessa.db.models import Project, Repository, Task
from harnessa.db.orchestration import Run, RunArtifact
from harnessa.db.retrieval import VerificationEvidence
from harnessa.db.specification import SpecificationInterview
from harnessa.services.retrieval_governance import safe_retrieval_content
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class VerificationEvidenceInput:
    organization_id: UUID
    project_id: UUID
    repository_id: UUID
    task_id: UUID
    run_id: UUID
    attempt: int
    fencing_token: int
    source_record_id: str
    source_revision: str
    outcome: Literal["passed", "failed", "manual_review_required", "unverified"]
    summary: str
    criteria: tuple[dict[str, str], ...]
    verifier_id: str
    artifact_digest: str | None = None


async def bind_run_source_revision(
    db: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID,
    repository_id: UUID,
    task_id: UUID,
    run_id: UUID,
    attempt: int,
    fencing_token: int,
    source_revision: str,
    workspace_manager: SpecificationWorkspaceManager,
) -> Run:
    """Bind the tested Git commit to a run exactly once from trusted execution code."""
    run = await db.scalar(select(Run).where(Run.id == run_id))
    task = await db.scalar(select(Task).where(Task.id == task_id))
    project = await db.scalar(select(Project).where(Project.id == project_id))
    interview = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == task_id)
    )
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
        )
    )
    if (
        run is None
        or task is None
        or project is None
        or repository is None
        or interview is None
        or run.task_id != task_id
        or run.project_id != project_id
        or run.attempt != attempt
        or run.fencing_token != fencing_token
        or task.project_id != project_id
        or project.organization_id != organization_id
        or interview.project_id != project_id
        or interview.repository_id != repository_id
    ):
        raise HTTPException(409, "Run source revision scope does not match the durable task")
    if not await workspace_manager.contains_revision(repository, source_revision):
        raise HTTPException(409, "Tested source revision is not a commit in the bound repository")
    if run.source_revision is not None and run.source_revision != source_revision:
        raise HTTPException(409, "Run source revision is already bound to a different commit")
    if run.source_revision is None:
        bound_id = await db.scalar(
            update(Run)
            .where(
                Run.id == run_id,
                Run.project_id == project_id,
                Run.task_id == task_id,
                Run.attempt == attempt,
                Run.fencing_token == fencing_token,
                Run.source_revision.is_(None),
            )
            .values(source_revision=source_revision)
            .returning(Run.id)
        )
        if bound_id is not None:
            run.source_revision = source_revision
        else:
            await db.refresh(run, attribute_names=["source_revision"])
            if run.source_revision != source_revision:
                raise HTTPException(409, "Run source revision was concurrently bound to a different commit")
    return run


async def record_verification_evidence(
    db: AsyncSession,
    evidence: VerificationEvidenceInput,
    *,
    workspace_manager: SpecificationWorkspaceManager,
) -> VerificationEvidence:
    """Persist trusted verifier output with task, repository, revision and run scope."""
    if not evidence.source_record_id.strip() or len(evidence.source_record_id) > 200:
        raise HTTPException(422, "Invalid verifier source record ID")
    if not evidence.source_revision.strip() or len(evidence.source_revision) > 200:
        raise HTTPException(422, "Invalid source revision")
    if not evidence.summary.strip() or len(evidence.summary) > 2000:
        raise HTTPException(422, "Verification summary must be 1–2000 characters")
    if not safe_retrieval_content(evidence.summary):
        raise HTTPException(422, "Verification summary contains secret-like content")
    if not evidence.verifier_id.strip() or len(evidence.verifier_id) > 120:
        raise HTTPException(422, "Invalid verifier identity")
    if not evidence.criteria or len(evidence.criteria) > 100:
        raise HTTPException(422, "Verification criteria must contain 1–100 results")
    if any(
        set(item) != {"criterion_id", "outcome", "summary"}
        or not item["criterion_id"].strip()
        or item["outcome"] not in {"passed", "failed", "manual_review_required", "unverified"}
        or len(item["summary"]) > 500
        for item in evidence.criteria
    ):
        raise HTTPException(422, "Invalid criterion result")
    if not safe_retrieval_content(json.dumps(evidence.criteria, sort_keys=True)):
        raise HTTPException(422, "Verification criteria contain secret-like content")
    if evidence.artifact_digest is not None and (
        len(evidence.artifact_digest) != 64
        or any(char not in "0123456789abcdef" for char in evidence.artifact_digest)
    ):
        raise HTTPException(422, "Artifact digest must be a lowercase SHA-256")

    run = await db.scalar(select(Run).where(Run.id == evidence.run_id))
    task = await db.scalar(select(Task).where(Task.id == evidence.task_id))
    project = await db.scalar(select(Project).where(Project.id == evidence.project_id))
    interview = await db.scalar(
        select(SpecificationInterview).where(SpecificationInterview.task_id == evidence.task_id)
    )
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == evidence.repository_id,
            Repository.project_id == evidence.project_id,
        )
    )
    if (
        run is None
        or task is None
        or repository is None
        or run.task_id != evidence.task_id
        or run.project_id != evidence.project_id
        or run.attempt != evidence.attempt
        or run.fencing_token != evidence.fencing_token
        or run.source_revision != evidence.source_revision
        or task.project_id != evidence.project_id
        or interview is None
        or interview.project_id != evidence.project_id
        or interview.repository_id != evidence.repository_id
        or project is None
        or project.organization_id != evidence.organization_id
    ):
        raise HTTPException(409, "Verification evidence scope does not match the durable run")
    if not await workspace_manager.contains_revision(repository, evidence.source_revision):
        raise HTTPException(409, "Verification source revision is not a commit in the bound repository")
    if evidence.artifact_digest is not None:
        artifact = await db.scalar(
            select(RunArtifact).where(
                RunArtifact.run_id == evidence.run_id,
                RunArtifact.fencing_token == evidence.fencing_token,
                RunArtifact.digest == evidence.artifact_digest,
                RunArtifact.project_id == evidence.project_id,
                RunArtifact.task_id == evidence.task_id,
                RunArtifact.organization_id == evidence.organization_id,
            )
        )
        if artifact is None:
            raise HTTPException(409, "Verification artifact is not published under this run fence")

    payload = {
        "organization_id": str(evidence.organization_id),
        "project_id": str(evidence.project_id),
        "repository_id": str(evidence.repository_id),
        "task_id": str(evidence.task_id),
        "run_id": str(evidence.run_id),
        "attempt": evidence.attempt,
        "fencing_token": evidence.fencing_token,
        "source_record_id": evidence.source_record_id,
        "source_revision": evidence.source_revision,
        "outcome": evidence.outcome,
        "summary": evidence.summary,
        "criteria": list(evidence.criteria),
        "verifier_id": evidence.verifier_id,
        "artifact_digest": evidence.artifact_digest,
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inserted_id = await db.scalar(
        insert(VerificationEvidence)
        .values(id=uuid4(), **payload, content_hash=content_hash)
        .on_conflict_do_nothing(
            index_elements=[
                VerificationEvidence.project_id,
                VerificationEvidence.source_record_id,
            ]
        )
        .returning(VerificationEvidence.id)
    )
    existing = await db.scalar(
        select(VerificationEvidence).where(
            VerificationEvidence.project_id == evidence.project_id,
            VerificationEvidence.source_record_id == evidence.source_record_id,
        )
    )
    if existing is None:
        raise HTTPException(409, "Verifier source record could not be persisted")
    if existing.content_hash != content_hash:
        raise HTTPException(409, "Verifier source record already exists with different content")
    if inserted_id is not None and existing.id != inserted_id:
        raise HTTPException(409, "Persisted verifier record changed during insertion")
    return existing
