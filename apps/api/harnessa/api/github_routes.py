"""Authenticated GitHub setup controls and the signed webhook ingress."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from harnessa.adapters.github_app import (
    APP_REQUIRED_PERMISSIONS,
    GitHubAppClient,
    GitHubAppError,
    check_permissions,
    resolve_secret_reference,
    verify_webhook_signature,
)
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    GitHubActorMappingCreate,
    GitHubActorMappingRead,
    GitHubAppConfigurationCreate,
    GitHubAppConfigurationRead,
    GitHubCommitStatusRequest,
    GitHubConnectionStatusRead,
    GitHubDeliveryRepairRequest,
    GitHubEffectQueuedRead,
    GitHubInstallationCreate,
    GitHubInstallationRead,
    GitHubPublishRequest,
    GitHubRepositoryLinkRead,
    GitHubRepositoryOptIn,
    GitHubWebhookReceipt,
)
from harnessa.db.github import (
    GitHubActorMapping,
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
    GitHubWebhookDelivery,
    GitHubWebhookOutbox,
)
from harnessa.db.models import Membership, Project, ProjectMembership, Repository, Task, User
from harnessa.db.orchestration import EffectIntent
from harnessa.db.review import ReviewBundle
from harnessa.db.session import get_db
from harnessa.services.authorization import require_membership
from harnessa.tracing import current_traceparent
from sqlalchemy import false, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["github"])
_HOOK_ID = re.compile(r"^[0-9]{1,20}$")
_DELIVERY_ID = re.compile(r"^[A-Fa-f0-9-]{1,80}$")
_EVENT_NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_CHECK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,99}$")


def _org_admin(membership: Membership | ProjectMembership) -> None:
    if not isinstance(membership, Membership) or membership.role != "administrator":
        raise HTTPException(403, "Organization administrator role required")


async def _organization_membership(db: AsyncSession, actor: User) -> Membership:
    membership = await require_membership(db, actor)
    if not isinstance(membership, Membership):
        raise HTTPException(403, "Organization access required")
    return membership


def _installation_read(row: GitHubInstallation) -> GitHubInstallationRead:
    return GitHubInstallationRead(
        id=row.id,
        app_configuration_id=row.app_configuration_id,
        installation_id=row.installation_id,
        account_id=row.account_id,
        account_login=row.account_login,
        account_type=row.account_type,
        repository_selection=row.repository_selection,
        granted_permissions=row.granted_permissions,
        active=row.active,
        checked_at=row.checked_at,
        last_error=row.last_error,
    )


def _app_read(row: GitHubAppConfiguration) -> GitHubAppConfigurationRead:
    return GitHubAppConfigurationRead(
        id=row.id,
        app_id=row.app_id,
        webhook_id=row.webhook_id,
        enabled=row.enabled,
        connection_status=row.connection_status,
        checked_at=row.checked_at,
        last_error=row.last_error,
    )


def _link_read(row: GitHubRepositoryLink) -> GitHubRepositoryLinkRead:
    return GitHubRepositoryLinkRead(
        id=row.id,
        repository_id=row.repository_id,
        installation_id=row.installation_id,
        github_repository_id=row.github_repository_id,
        full_name=row.full_name,
        opted_in=row.opted_in,
        required_checks=row.required_checks,
        topic_label=row.topic_label,
        archive_policy=row.archive_policy,
        setup_status=row.setup_status,
        last_synced_at=row.last_synced_at,
        last_error=row.last_error,
    )


def _client() -> GitHubAppClient:
    try:
        return GitHubAppClient()
    except GitHubAppError as exc:
        raise HTTPException(503, str(exc)) from exc


async def _refresh_installation(
    db: AsyncSession,
    *,
    app_config: GitHubAppConfiguration,
    installation_id: int,
    client: GitHubAppClient,
) -> GitHubInstallation:
    try:
        remote = await client.get_installation(
            app_id=app_config.app_id,
            private_key_reference=app_config.private_key_secret_ref,
            installation_id=installation_id,
        )
    except GitHubAppError as exc:
        app_config.connection_status = "error"
        app_config.checked_at = datetime.now(UTC)
        app_config.last_error = str(exc)
        await db.commit()
        raise HTTPException(502, str(exc)) from exc
    if remote.get("app_id") != app_config.app_id:
        raise HTTPException(409, "Installation belongs to a different GitHub App")
    account = remote.get("account")
    if not isinstance(account, dict) or not isinstance(account.get("login"), str):
        raise HTTPException(502, "GitHub installation response omitted its account identity")
    permissions_raw = remote.get("permissions")
    permissions = (
        {str(k): str(v) for k, v in permissions_raw.items() if isinstance(k, str)}
        if isinstance(permissions_raw, dict)
        else {}
    )
    missing = check_permissions(permissions, APP_REQUIRED_PERMISSIONS)
    selection = remote.get("repository_selection")
    if selection not in {"all", "selected"}:
        selection = "unknown"
    row = await db.scalar(
        select(GitHubInstallation)
        .where(
            GitHubInstallation.app_configuration_id == app_config.id,
            GitHubInstallation.installation_id == installation_id,
        )
        .with_for_update()
    )
    if row is None:
        row = GitHubInstallation(
            organization_id=app_config.organization_id,
            app_configuration_id=app_config.id,
            installation_id=installation_id,
            account_login=account["login"],
            account_type=str(account.get("type") or "unknown"),
            repository_selection=selection,
            granted_permissions=permissions,
            active=False,
        )
        db.add(row)
    row.account_id = account.get("id") if isinstance(account.get("id"), int) else None
    row.account_login = account["login"]
    row.account_type = str(account.get("type") or "unknown")
    row.repository_selection = selection
    row.granted_permissions = permissions
    row.active = not bool(remote.get("suspended_at")) and not missing
    row.checked_at = datetime.now(UTC)
    row.last_error = "Missing GitHub App permissions: " + ", ".join(missing) if missing else None
    app_config.connection_status = (
        "ready" if row.active else ("permission_missing" if missing else "suspended")
    )
    app_config.checked_at = datetime.now(UTC)
    app_config.last_error = row.last_error
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/github/apps", response_model=GitHubAppConfigurationRead, status_code=201)
async def register_github_app(
    body: GitHubAppConfigurationCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubAppConfigurationRead:
    membership = await _organization_membership(db, actor)
    _org_admin(membership)
    for ref in (body.private_key_secret_ref, body.webhook_secret_ref):
        try:
            resolve_secret_reference(ref)
        except GitHubAppError as exc:
            raise HTTPException(422, str(exc)) from exc
    row = GitHubAppConfiguration(
        organization_id=membership.organization_id,
        app_id=body.app_id,
        webhook_id=body.webhook_id,
        private_key_secret_ref=body.private_key_secret_ref,
        webhook_secret_ref=body.webhook_secret_ref,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "GitHub App or webhook ID is already configured") from exc
    await db.refresh(row)
    return _app_read(row)


@router.get("/github/status", response_model=GitHubConnectionStatusRead)
async def github_status(
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubConnectionStatusRead:
    membership = await _organization_membership(db, actor)
    admin = membership.role == "administrator"
    app_rows = list(
        (
            await db.scalars(
                select(GitHubAppConfiguration)
                .where(GitHubAppConfiguration.organization_id == membership.organization_id)
                .order_by(GitHubAppConfiguration.created_at)
            )
        ).all()
    )
    installation_rows = list(
        (
            await db.scalars(
                select(GitHubInstallation)
                .where(GitHubInstallation.organization_id == membership.organization_id)
                .order_by(GitHubInstallation.account_login)
            )
        ).all()
    )
    if admin:
        links_query = select(GitHubRepositoryLink).where(
            GitHubRepositoryLink.organization_id == membership.organization_id
        )
        mapping_query = select(GitHubActorMapping).where(
            GitHubActorMapping.organization_id == membership.organization_id
        )
    else:
        visible_project_ids = select(ProjectMembership.project_id).where(
            ProjectMembership.user_id == actor.id
        )
        links_query = select(GitHubRepositoryLink).where(
            GitHubRepositoryLink.organization_id == membership.organization_id,
            GitHubRepositoryLink.project_id.in_(visible_project_ids),
        )
        mapping_query = select(GitHubActorMapping).where(false())
    link_rows = list((await db.scalars(links_query.order_by(GitHubRepositoryLink.full_name))).all())
    mapping_rows = list(
        (await db.scalars(mapping_query.order_by(GitHubActorMapping.github_login))).all()
    )
    # Delivery payloads are never returned to the browser; only bounded health counters/metadata.
    if admin:
        count_rows = (
            await db.execute(
                select(GitHubWebhookDelivery.status, func.count())
                .where(GitHubWebhookDelivery.organization_id == membership.organization_id)
                .group_by(GitHubWebhookDelivery.status)
            )
        ).all()
        counts: dict[str, int] = {str(key): int(value) for key, value in count_rows}
        deliveries = list(
            (
                await db.execute(
                    select(
                        GitHubWebhookDelivery.id,
                        GitHubWebhookDelivery.delivery_id,
                        GitHubWebhookDelivery.event_name,
                        GitHubWebhookDelivery.action,
                        GitHubWebhookDelivery.status,
                        GitHubWebhookDelivery.last_error,
                        GitHubWebhookDelivery.created_at,
                    )
                    .where(GitHubWebhookDelivery.organization_id == membership.organization_id)
                    .order_by(GitHubWebhookDelivery.created_at.desc())
                    .limit(20)
                )
            ).all()
        )
    else:
        counts, deliveries = {}, []
    return GitHubConnectionStatusRead(
        configured_apps=[_app_read(row) for row in app_rows] if admin else [],
        installations=[_installation_read(row) for row in installation_rows] if admin else [],
        repository_links=[_link_read(row) for row in link_rows],
        actor_mappings=[GitHubActorMappingRead.model_validate(row) for row in mapping_rows],
        webhook_health={str(key): int(value) for key, value in counts.items()},
        recent_deliveries=[
            {
                "id": str(row.id),
                "delivery_id": row.delivery_id,
                "event_name": row.event_name,
                "action": row.action,
                "status": row.status,
                "last_error": row.last_error,
                "created_at": row.created_at,
            }
            for row in deliveries
        ],
    )


@router.post("/github/installations", response_model=GitHubInstallationRead, status_code=201)
async def register_github_installation(
    body: GitHubInstallationCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationRead:
    membership = await _organization_membership(db, actor)
    _org_admin(membership)
    app_config = await db.scalar(
        select(GitHubAppConfiguration)
        .where(
            GitHubAppConfiguration.id == body.app_configuration_id,
            GitHubAppConfiguration.organization_id == membership.organization_id,
            GitHubAppConfiguration.enabled.is_(True),
        )
        .with_for_update()
    )
    if app_config is None:
        raise HTTPException(404, "Enabled GitHub App configuration not found")
    return _installation_read(
        await _refresh_installation(
            db,
            app_config=app_config,
            installation_id=body.installation_id,
            client=_client(),
        )
    )


@router.post(
    "/github/installations/{installation_id}/refresh", response_model=GitHubInstallationRead
)
async def refresh_github_installation(
    installation_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationRead:
    membership = await _organization_membership(db, actor)
    _org_admin(membership)
    row = await db.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == installation_id,
            GitHubInstallation.organization_id == membership.organization_id,
        )
    )
    if row is None:
        raise HTTPException(404, "GitHub installation not found")
    app_config = await db.get(GitHubAppConfiguration, row.app_configuration_id)
    if app_config is None:
        raise HTTPException(409, "GitHub App configuration was removed")
    return _installation_read(
        await _refresh_installation(
            db,
            app_config=app_config,
            installation_id=row.installation_id,
            client=_client(),
        )
    )


@router.post(
    "/projects/{project_id}/repositories/{repository_id}/github",
    response_model=GitHubRepositoryLinkRead,
    status_code=201,
)
async def opt_in_github_repository(
    project_id: UUID,
    repository_id: UUID,
    body: GitHubRepositoryOptIn,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubRepositoryLinkRead:
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to opt in a repository")
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == repository_id, Repository.project_id == project_id
        )
    )
    project = await db.get(Project, project_id)
    installation = await db.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == body.installation_id,
            GitHubInstallation.organization_id == (project.organization_id if project else None),
            GitHubInstallation.active.is_(True),
        )
    )
    if repository is None or project is None:
        raise HTTPException(404, "Repository not found in this project")
    if installation is None:
        raise HTTPException(409, "A verified active GitHub installation is required")
    target = repository.target_id.split("/")
    if len(target) != 2 or any(not _REPOSITORY_NAME.fullmatch(part) for part in target):
        raise HTTPException(409, "Repository target must be an explicit owner/repository name")
    if not body.required_checks or len(set(body.required_checks)) != len(body.required_checks):
        raise HTTPException(422, "Required check names must be non-empty and unique")
    if any(not _CHECK_NAME.fullmatch(name) for name in body.required_checks):
        raise HTTPException(422, "Required check names contain unsupported characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}", body.topic_label):
        raise HTTPException(422, "Issue topic label contains unsupported characters")
    app_config = await db.get(GitHubAppConfiguration, installation.app_configuration_id)
    if app_config is None or not app_config.enabled:
        raise HTTPException(409, "GitHub App configuration is disabled")
    try:
        remote = await _client().request_installation(
            app_id=app_config.app_id,
            private_key_reference=app_config.private_key_secret_ref,
            installation_id=installation.installation_id,
            repository_id=body.github_repository_id,
            method="GET",
            path=f"/repos/{target[0]}/{target[1]}",
            permissions={"metadata": "read"},
        )
    except GitHubAppError as exc:
        raise HTTPException(502, str(exc)) from exc
    if (
        not isinstance(remote, dict)
        or remote.get("id") != body.github_repository_id
        or not isinstance(remote.get("full_name"), str)
        or remote["full_name"].casefold() != repository.target_id.casefold()
    ):
        raise HTTPException(
            409, "GitHub repository does not match the explicitly registered target"
        )
    row = await db.scalar(
        select(GitHubRepositoryLink)
        .where(GitHubRepositoryLink.repository_id == repository.id)
        .with_for_update()
    )
    if row is not None and (
        row.installation_id != installation.id
        or row.github_repository_id != body.github_repository_id
    ):
        raise HTTPException(409, "Repository is already linked to a different GitHub target")
    if row is None:
        row = GitHubRepositoryLink(
            organization_id=project.organization_id,
            project_id=project.id,
            repository_id=repository.id,
            installation_id=installation.id,
            github_repository_id=body.github_repository_id,
            owner=target[0],
            name=target[1],
            full_name=remote["full_name"],
            opted_in=True,
            authorized_by=actor.id,
            authorized_at=datetime.now(UTC),
            required_checks=body.required_checks,
            topic_label=body.topic_label,
            archive_policy=body.archive_policy,
        )
        db.add(row)
    else:
        row.owner = target[0]
        row.name = target[1]
        row.full_name = remote["full_name"]
        row.opted_in = True
        row.authorized_by = actor.id
        row.authorized_at = datetime.now(UTC)
        row.required_checks = body.required_checks
        row.topic_label = body.topic_label
        row.archive_policy = body.archive_policy
        row.setup_status = "ready"
        row.last_error = None
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            409, "GitHub repository is already linked in this installation"
        ) from exc
    await db.refresh(row)
    return _link_read(row)


@router.delete("/projects/{project_id}/repositories/{repository_id}/github", status_code=204)
async def opt_out_github_repository(
    project_id: UUID,
    repository_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    membership = await require_membership(db, actor, project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to opt out a repository")
    row = await db.scalar(
        select(GitHubRepositoryLink)
        .where(
            GitHubRepositoryLink.repository_id == repository_id,
            GitHubRepositoryLink.project_id == project_id,
        )
        .with_for_update()
    )
    if row is not None:
        row.opted_in = False
        row.setup_status = "stale"
        row.last_error = "Repository opt-in was disabled by a project operator"
        await db.commit()
    return Response(status_code=204)


@router.post("/github/actors", response_model=GitHubActorMappingRead, status_code=201)
async def map_github_actor(
    body: GitHubActorMappingCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubActorMappingRead:
    membership = await _organization_membership(db, actor)
    _org_admin(membership)
    installation = await db.scalar(
        select(GitHubInstallation).where(
            GitHubInstallation.id == body.installation_id,
            GitHubInstallation.organization_id == membership.organization_id,
            GitHubInstallation.active.is_(True),
        )
    )
    user_membership = await db.scalar(
        select(Membership).where(
            Membership.organization_id == membership.organization_id,
            Membership.user_id == body.user_id,
        )
    )
    if installation is None or user_membership is None:
        raise HTTPException(404, "Active installation or organization user not found")
    row = await db.scalar(
        select(GitHubActorMapping)
        .where(
            GitHubActorMapping.installation_id == installation.id,
            GitHubActorMapping.github_user_id == body.github_user_id,
        )
        .with_for_update()
    )
    if row is None:
        row = GitHubActorMapping(
            organization_id=membership.organization_id,
            installation_id=installation.id,
            github_user_id=body.github_user_id,
            github_login=body.github_login,
            user_id=body.user_id,
            active=True,
        )
        db.add(row)
    else:
        row.github_login = body.github_login
        row.user_id = body.user_id
        row.active = True
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "GitHub actor mapping conflicts with an existing mapping") from exc
    await db.refresh(row)
    return GitHubActorMappingRead.model_validate(row)


@router.post("/github/deliveries/{delivery_row_id}/retry", status_code=202)
async def retry_github_delivery(
    delivery_row_id: UUID,
    body: GitHubDeliveryRepairRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    membership = await _organization_membership(db, actor)
    _org_admin(membership)
    delivery = await db.scalar(
        select(GitHubWebhookDelivery)
        .where(
            GitHubWebhookDelivery.id == delivery_row_id,
            GitHubWebhookDelivery.organization_id == membership.organization_id,
        )
        .with_for_update()
    )
    if delivery is None or delivery.status not in {"failed", "ignored"}:
        raise HTTPException(409, "Only failed or ignored GitHub deliveries can be repaired")
    outbox = await db.scalar(
        select(GitHubWebhookOutbox)
        .where(GitHubWebhookOutbox.delivery_id == delivery.id)
        .with_for_update()
    )
    if outbox is None:
        raise HTTPException(409, "Webhook processing intent is missing")
    repairs = delivery.result.get("repairs", [])
    if not isinstance(repairs, list):
        repairs = []
    repairs.append(
        {"actor_id": str(actor.id), "reason": body.reason, "at": datetime.now(UTC).isoformat()}
    )
    delivery.result = {**delivery.result, "repairs": repairs[-20:]}
    delivery.status = "pending"
    delivery.last_error = None
    outbox.status = "pending"
    outbox.attempts = 0
    outbox.next_attempt_at = None
    outbox.last_error = None
    outbox.lease_owner = None
    outbox.lease_until = None
    await db.commit()
    return {"status": "queued"}


@router.post(
    "/tasks/{task_id}/github-publish", response_model=GitHubEffectQueuedRead, status_code=202
)
async def queue_github_review_publication(
    task_id: UUID,
    body: GitHubPublishRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubEffectQueuedRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to publish a review bundle")
    bundle = await db.scalar(
        select(ReviewBundle)
        .where(ReviewBundle.task_id == task_id)
        .order_by(ReviewBundle.created_at.desc())
        .with_for_update()
        .limit(1)
    )
    if bundle is None:
        raise HTTPException(404, "No review bundle is available")
    if bundle.status != "prepared" or task.stage != "HUMAN_REVIEW":
        raise HTTPException(409, "Only the current prepared review bundle can be published")
    from harnessa.api.review_routes import _bundle_stale_reason

    stale_reason = await _bundle_stale_reason(db, bundle)
    if stale_reason:
        bundle.status = "invalidated"
        bundle.invalidated_reason = stale_reason
        await db.commit()
        raise HTTPException(409, "Review bundle is stale and must be regenerated")
    link = await db.scalar(
        select(GitHubRepositoryLink).where(
            GitHubRepositoryLink.repository_id == bundle.repository_id,
            GitHubRepositoryLink.project_id == bundle.project_id,
            GitHubRepositoryLink.opted_in.is_(True),
            GitHubRepositoryLink.setup_status == "ready",
        )
    )
    if link is None:
        raise HTTPException(409, "Repository does not have an active GitHub opt-in")
    installation = await db.get(GitHubInstallation, link.installation_id)
    if installation is None or not installation.active:
        raise HTTPException(409, "GitHub installation is inactive")
    stable_key = f"github-review-publish:{bundle.id}"
    existing = await db.scalar(select(EffectIntent).where(EffectIntent.stable_key == stable_key))
    if existing is None:
        project = await db.get(Project, bundle.project_id)
        if project is None:
            raise HTTPException(404, "Review project not found")
        db.add(
            EffectIntent(
                project_id=project.id,
                task_id=task.id,
                kind="github.review_bundle.publish",
                stable_key=stable_key,
                payload={
                    "bundle_id": str(bundle.id),
                    "repository_link_id": str(link.id),
                    "bundle_digest": bundle.bundle_digest,
                    "idempotency_key": body.idempotency_key,
                    "requested_by": str(actor.id),
                },
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(
                select(EffectIntent).where(EffectIntent.stable_key == stable_key)
            )
            if existing is None:
                raise HTTPException(
                    409, "GitHub publication intent conflicted and could not be recovered"
                )
    else:
        await db.commit()
    return GitHubEffectQueuedRead(queued=True, stable_keys=[stable_key])


@router.post(
    "/tasks/{task_id}/github-issue", response_model=GitHubEffectQueuedRead, status_code=202
)
async def queue_github_issue_creation(
    task_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubEffectQueuedRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to create a GitHub issue")
    links = (
        await db.scalars(
            select(GitHubRepositoryLink).where(
                GitHubRepositoryLink.project_id == task.project_id,
                GitHubRepositoryLink.opted_in.is_(True),
                GitHubRepositoryLink.setup_status == "ready",
            )
        )
    ).all()
    if len(links) != 1:
        raise HTTPException(
            409,
            "GitHub issue creation requires exactly one ready repository opt-in for the project",
        )
    link = links[0]
    installation = await db.get(GitHubInstallation, link.installation_id)
    if installation is None or not installation.active:
        raise HTTPException(409, "GitHub installation is inactive")
    stable_key = f"github-issue-create:{link.id}:{task.id}"
    existing = await db.scalar(select(EffectIntent).where(EffectIntent.stable_key == stable_key))
    if existing is None:
        project = await db.get(Project, task.project_id)
        if project is None:
            raise HTTPException(404, "Task project not found")
        db.add(
            EffectIntent(
                project_id=project.id,
                task_id=task.id,
                kind="github.issue.create",
                stable_key=stable_key,
                payload={
                    "repository_link_id": str(link.id),
                    "task_id": str(task.id),
                    "title": task.title,
                    "body": task.description,
                },
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(
                select(EffectIntent).where(EffectIntent.stable_key == stable_key)
            )
            if existing is None:
                raise HTTPException(
                    409, "GitHub issue intent conflicted and could not be recovered"
                )
    return GitHubEffectQueuedRead(queued=True, stable_keys=[stable_key])


@router.post(
    "/tasks/{task_id}/github-status",
    response_model=GitHubEffectQueuedRead,
    status_code=202,
)
async def queue_github_commit_status(
    task_id: UUID,
    body: GitHubCommitStatusRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubEffectQueuedRead:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    membership = await require_membership(db, actor, task.project_id)
    if membership.role not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to publish GitHub status")
    sync = await db.scalar(
        select(GitHubSyncObject)
        .join(
            GitHubRepositoryLink,
            GitHubRepositoryLink.id == GitHubSyncObject.repository_link_id,
        )
        .where(
            GitHubSyncObject.task_id == task.id,
            GitHubSyncObject.object_kind == "pull_request",
            GitHubSyncObject.head_sha.is_not(None),
            GitHubRepositoryLink.project_id == task.project_id,
            GitHubRepositoryLink.opted_in.is_(True),
            GitHubRepositoryLink.setup_status == "ready",
        )
        .order_by(GitHubSyncObject.created_at.desc())
        .limit(1)
    )
    if sync is None or not isinstance(sync.head_sha, str):
        raise HTTPException(409, "Task has no current tracked GitHub pull request head")
    link = await db.get(GitHubRepositoryLink, sync.repository_link_id)
    if link is None:
        raise HTTPException(409, "Tracked GitHub repository is unavailable")
    installation = await db.get(GitHubInstallation, link.installation_id)
    if installation is None or not installation.active:
        raise HTTPException(409, "GitHub installation is inactive")
    context = f"harnessa/task/{task.id.hex[:24]}"
    target_url = str(body.target_url) if body.target_url is not None else None
    status_digest = hashlib.sha256(
        json.dumps(
            [sync.head_sha, body.state, body.description, target_url],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:20]
    stable_key = f"github-status:{link.github_repository_id}:{task.id}:{status_digest}"
    existing = await db.scalar(select(EffectIntent).where(EffectIntent.stable_key == stable_key))
    if existing is None:
        project = await db.get(Project, task.project_id)
        if project is None:
            raise HTTPException(404, "Task project not found")
        db.add(
            EffectIntent(
                project_id=project.id,
                task_id=task.id,
                kind="github.commit_status.publish",
                stable_key=stable_key,
                payload={
                    "repository_link_id": str(link.id),
                    "task_id": str(task.id),
                    "sha": sync.head_sha,
                    "state": body.state,
                    "description": body.description,
                    "context": context,
                    "target_url": target_url,
                    "task_version": task.version,
                },
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(
                select(EffectIntent).where(EffectIntent.stable_key == stable_key)
            )
            if existing is None:
                raise HTTPException(
                    409, "GitHub status intent conflicted and could not be recovered"
                )
    return GitHubEffectQueuedRead(queued=True, stable_keys=[stable_key])


@router.post("/webhooks/github", response_model=GitHubWebhookReceipt, status_code=202)
async def receive_github_webhook(
    request: Request,
    response: Response,
    hook_id: str = Header(alias="X-GitHub-Hook-ID"),
    delivery_id: str = Header(alias="X-GitHub-Delivery"),
    event_name: str = Header(alias="X-GitHub-Event"),
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db),
) -> GitHubWebhookReceipt:
    if not _HOOK_ID.fullmatch(hook_id) or not _DELIVERY_ID.fullmatch(delivery_id):
        raise HTTPException(400, "GitHub hook or delivery identity is invalid")
    if not _EVENT_NAME.fullmatch(event_name):
        raise HTTPException(400, "GitHub event name is invalid")
    declared_length = request.headers.get("content-length")
    if (
        declared_length
        and declared_length.isdigit()
        and int(declared_length) > settings.github_webhook_max_bytes
    ):
        raise HTTPException(413, "GitHub webhook exceeds the configured size limit")
    app_config = await db.scalar(
        select(GitHubAppConfiguration).where(
            GitHubAppConfiguration.webhook_id == int(hook_id),
            GitHubAppConfiguration.enabled.is_(True),
        )
    )
    if app_config is None:
        raise HTTPException(404, "GitHub webhook is not configured")
    app_configuration_id = app_config.id
    try:
        secret = resolve_secret_reference(app_config.webhook_secret_ref)
    except GitHubAppError as exc:
        raise HTTPException(503, str(exc)) from exc
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > settings.github_webhook_max_bytes:
            raise HTTPException(413, "GitHub webhook exceeds the configured size limit")
    raw_body = bytes(raw)
    if not verify_webhook_signature(raw_body, signature, secret):
        raise HTTPException(401, "GitHub webhook signature is invalid")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "GitHub webhook payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "GitHub webhook payload must be an object")
    installation = payload.get("installation")
    installation_id = installation.get("id") if isinstance(installation, dict) else None
    repository = payload.get("repository")
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    action = payload.get("action")
    digest = hashlib.sha256(raw_body).hexdigest()
    delivery = GitHubWebhookDelivery(
        organization_id=app_config.organization_id,
        app_configuration_id=app_config.id,
        delivery_id=delivery_id,
        event_name=event_name,
        action=action if isinstance(action, str) else None,
        installation_number=installation_id if isinstance(installation_id, int) else None,
        github_repository_id=repository_id if isinstance(repository_id, int) else None,
        payload_sha256=digest,
        payload_reference=f"sha256:{digest}",
        payload=payload,
        traceparent=current_traceparent(),
        result={},
    )
    db.add(delivery)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(GitHubWebhookDelivery).where(
                GitHubWebhookDelivery.app_configuration_id == app_configuration_id,
                GitHubWebhookDelivery.delivery_id == delivery_id,
            )
        )
        if existing is None or existing.payload_sha256 != digest:
            raise HTTPException(409, "GitHub delivery ID was reused with different payload bytes")
        response.headers["X-Harnessa-Webhook-Duplicate"] = "true"
        return GitHubWebhookReceipt(duplicate=True)
    db.add(
        GitHubWebhookOutbox(
            organization_id=app_config.organization_id,
            delivery_id=delivery.id,
        )
    )
    await db.commit()
    return GitHubWebhookReceipt()
