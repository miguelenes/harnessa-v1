"""Authorize and apply the supported issue-to-interview GitHub webhook commands."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from harnessa.adapters.github_app import (
    APP_REQUIRED_PERMISSIONS,
    GitHubAppClient,
    GitHubAppError,
    check_permissions,
)
from harnessa.config import settings
from harnessa.db.github import (
    GitHubActorMapping,
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
)
from harnessa.db.models import Membership, ProjectMembership, Task, User
from harnessa.db.orchestration import EffectIntent, Run, TaskTransition
from harnessa.db.review import (
    IntegrationEvidence,
    PostIntegrationStep,
    ReviewBundle,
    ReviewDecision,
    VerificationExecution,
)
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview, SpecificationQuestion
from harnessa.services.github_webhooks import DeliveryEnvelope, DeliveryOutcome
from harnessa.services.specifications import answer_question, create_interview
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ANSWER_COMMAND = re.compile(
    r"^/harnessa answer (?P<question>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}) r(?P<revision>[1-9][0-9]{0,8}):[ \t]*(?P<answer>[\s\S]{1,8000})$"
)
logger = logging.getLogger("harnessa.github")


class GitHubWebhookProcessor:
    """Apply only explicitly opted-in issue commands from mapped local operators."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
        self.session_factory = session_factory

    async def process(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        if delivery.event_name == "issues":
            if delivery.action == "opened":
                return await self._issue_opened(delivery)
            if delivery.action == "edited":
                return await self._issue_edited(delivery)
        if delivery.event_name == "issue_comment" and delivery.action == "created":
            return await self._issue_comment(delivery)
        if delivery.event_name == "pull_request" and delivery.action in {
            "opened",
            "reopened",
            "synchronize",
            "edited",
            "closed",
        }:
            return await self._pull_request_changed(delivery)
        if delivery.event_name == "check_run" and delivery.action == "completed":
            return await self._checks_completed(delivery, "check_run")
        if delivery.event_name == "check_suite" and delivery.action == "completed":
            return await self._checks_completed(delivery, "check_suite")
        if delivery.event_name == "pull_request_review" and delivery.action in {
            "submitted",
            "dismissed",
        }:
            return await self._review_changed(delivery)
        if delivery.event_name in {"installation", "installation_repositories"}:
            return await self._installation_state(delivery)
        # Review/check payloads still require their own current-state reconciliation.
        return DeliveryOutcome("ignored", {"reason": "unsupported_or_non_authoritative_event"})

    async def reconcile_tracked_pull_requests(self, *, limit: int = 25) -> int:
        """Poll a bounded set of opted-in PRs to recover missed/reordered webhooks."""
        if limit < 1 or limit > 100:
            raise ValueError("GitHub reconciliation limit must be between 1 and 100")
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GitHubRepositoryLink, GitHubInstallation, GitHubAppConfiguration, GitHubSyncObject)
                    .join(GitHubInstallation, GitHubInstallation.id == GitHubRepositoryLink.installation_id)
                    .join(
                        GitHubAppConfiguration,
                        GitHubAppConfiguration.id == GitHubInstallation.app_configuration_id,
                    )
                    .join(
                        GitHubSyncObject,
                        GitHubSyncObject.repository_link_id == GitHubRepositoryLink.id,
                    )
                    .where(
                        GitHubRepositoryLink.opted_in.is_(True),
                        GitHubRepositoryLink.setup_status == "ready",
                        GitHubInstallation.active.is_(True),
                        GitHubAppConfiguration.enabled.is_(True),
                        GitHubSyncObject.object_kind == "pull_request",
                        GitHubSyncObject.object_number.is_not(None),
                    )
                    .order_by(GitHubSyncObject.reconciled_at.asc().nulls_first())
                    .limit(limit)
                )
            ).all()
            snapshots = [
                (
                    link.organization_id,
                    app.id,
                    installation.installation_id,
                    link.github_repository_id,
                    sync.github_object_id,
                    sync.object_number,
                )
                for link, installation, app, sync in rows
            ]
        reconciled = 0
        for organization_id, app_id, installation_number, repository_id, object_id, number in snapshots:
            try:
                if not object_id.isdecimal():
                    continue
                async with self.session_factory() as db:
                    app = await db.get(GitHubAppConfiguration, app_id)
                    if app is None:
                        continue
                    scope = await db.scalar(
                        select(GitHubRepositoryLink).where(
                            GitHubRepositoryLink.github_repository_id == repository_id,
                            GitHubRepositoryLink.organization_id == organization_id,
                            GitHubRepositoryLink.opted_in.is_(True),
                        )
                    )
                    if scope is None:
                        continue
                    payload = {
                        "installation": {"id": installation_number},
                        "repository": {"id": repository_id},
                        "number": number,
                        "pull_request": {"id": int(object_id)},
                    }
                    delivery = DeliveryEnvelope(
                        id=UUID(int=0),
                        organization_id=organization_id,
                        app_configuration_id=app.id,
                        event_name="pull_request",
                        action="edited",
                        installation_number=installation_number,
                        github_repository_id=repository_id,
                        payload=payload,
                        delivery_id=f"reconcile-pr-{repository_id}-{number}",
                    )
                    await self._pull_request_changed(delivery)
                    current_head = await db.scalar(
                        select(GitHubSyncObject.head_sha).where(
                            GitHubSyncObject.repository_link_id == scope.id,
                            GitHubSyncObject.object_kind == "pull_request",
                            GitHubSyncObject.object_number == number,
                        )
                    )
                    if isinstance(current_head, str):
                        check_payload = {
                            **payload,
                            "check_run": {
                                "head_sha": current_head,
                                "pull_requests": [{"number": number}],
                            },
                        }
                        await self._checks_completed(
                            DeliveryEnvelope(
                                id=UUID(int=0),
                                organization_id=organization_id,
                                app_configuration_id=app.id,
                                event_name="check_run",
                                action="completed",
                                installation_number=installation_number,
                                github_repository_id=repository_id,
                                payload=check_payload,
                                delivery_id=f"reconcile-check-{repository_id}-{number}-{current_head}",
                            ),
                            "check_run",
                        )
                    installation = await db.get(GitHubInstallation, scope.installation_id)
                    app = await db.get(GitHubAppConfiguration, app_id)
                    if installation is not None and app is not None:
                        remote_reviews = await GitHubAppClient().request_installation(
                            app_id=app.app_id,
                            private_key_reference=app.private_key_secret_ref,
                            installation_id=installation.installation_id,
                            repository_id=repository_id,
                            method="GET",
                            path=f"/repos/{scope.owner}/{scope.name}/pulls/{number}/reviews",
                            params={"per_page": 100},
                            permissions={"pull_requests": "read"},
                        )
                        if isinstance(remote_reviews, list):
                            actor_mappings = (
                                await db.scalars(
                                    select(GitHubActorMapping).where(
                                        GitHubActorMapping.installation_id == installation.id,
                                        GitHubActorMapping.active.is_(True),
                                    )
                                )
                            ).all()
                            actor_by_github_id = {
                                mapping.github_user_id: mapping.user_id
                                for mapping in actor_mappings
                            }
                            latest_reviews = _latest_review_state_by_actor(
                                remote_reviews, actor_by_github_id, current_head or ""
                            )
                            pending_reviews: list[tuple[dict[str, Any], str]] = []
                            for actor_id, review in latest_reviews.items():
                                review_id = review.get("id")
                                state = str(review.get("state", "")).casefold()
                                if not isinstance(review_id, int) or state not in {
                                    "approved",
                                    "changes_requested",
                                    "dismissed",
                                }:
                                    continue
                                action = "dismissed" if state == "dismissed" else "submitted"
                                key = f"github-review:{installation.installation_id}:{review_id}:{action}"
                                prior = await db.scalar(
                                    select(ReviewDecision.id).where(
                                        ReviewDecision.actor_id == actor_id,
                                        ReviewDecision.idempotency_key == key,
                                    )
                                )
                                if prior is None:
                                    pending_reviews.append((review, action))
                            pending_reviews.sort(
                                key=lambda item: str(
                                    item[0].get("submitted_at")
                                    or item[0].get("created_at")
                                    or ""
                                ),
                                reverse=True,
                            )
                            for review, action in pending_reviews[:10]:
                                review_payload = {
                                    **payload,
                                    "review": review,
                                }
                                await self._review_changed(
                                    DeliveryEnvelope(
                                        id=UUID(int=0),
                                        organization_id=organization_id,
                                        app_configuration_id=app.id,
                                        event_name="pull_request_review",
                                        action=action,
                                        installation_number=installation_number,
                                        github_repository_id=repository_id,
                                        payload=review_payload,
                                        delivery_id=f"reconcile-review-{repository_id}-{number}-{review['id']}-{action}",
                                    )
                                )
                reconciled += 1
            except Exception:
                # A failed remote read remains eligible for the next bounded pass.
                logger.exception(
                    "GitHub pull request reconciliation failed for repository %s PR %s",
                    repository_id,
                    number,
                )
                continue
        return reconciled

    async def reconcile_installation_access(self, *, limit: int = 25) -> int:
        """Refresh installation grants and explicitly opted-in repository access."""
        if limit < 1 or limit > 100:
            raise ValueError("GitHub installation reconciliation limit must be between 1 and 100")
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GitHubInstallation, GitHubAppConfiguration)
                    .join(
                        GitHubAppConfiguration,
                        GitHubAppConfiguration.id == GitHubInstallation.app_configuration_id,
                    )
                    .where(
                        GitHubAppConfiguration.enabled.is_(True),
                    )
                    .order_by(GitHubInstallation.checked_at.asc().nulls_first())
                    .limit(limit)
                )
            ).all()
            snapshots = [
                (installation.id, installation.installation_id, app.id, app.app_id,
                 app.private_key_secret_ref, installation.organization_id)
                for installation, app in rows
            ]
        reconciled = 0
        for installation_uuid, installation_number, app_uuid, app_id, key_ref, organization_id in snapshots:
            client = GitHubAppClient()
            try:
                remote = await client.get_installation(
                    app_id=app_id,
                    private_key_reference=key_ref,
                    installation_id=installation_number,
                )
            except GitHubAppError as exc:
                async with self.session_factory() as db:
                    installation = await db.get(GitHubInstallation, installation_uuid)
                    if exc.status_code == 404 and installation is not None:
                        installation.active = False
                        installation.checked_at = datetime.now(UTC)
                        installation.last_error = "GitHub installation no longer exists"
                        links = (
                            await db.scalars(
                                select(GitHubRepositoryLink).where(
                                    GitHubRepositoryLink.installation_id == installation_uuid,
                                    GitHubRepositoryLink.opted_in.is_(True),
                                )
                            )
                        ).all()
                        for link in links:
                            link.opted_in = False
                            link.setup_status = "revoked"
                            link.last_error = installation.last_error
                        await db.commit()
                        reconciled += 1
                if exc.status_code != 404:
                    logger.warning(
                        "GitHub installation reconciliation failed for installation %s",
                        installation_number,
                        exc_info=True,
                    )
                continue
            permissions_raw = remote.get("permissions")
            permissions = (
                {str(key): str(value) for key, value in permissions_raw.items() if isinstance(key, str)}
                if isinstance(permissions_raw, dict)
                else {}
            )
            missing = check_permissions(permissions, APP_REQUIRED_PERMISSIONS)
            unavailable = bool(remote.get("suspended_at")) or bool(missing)
            issue = (
                "GitHub installation is suspended"
                if remote.get("suspended_at")
                else ("Missing GitHub App permissions: " + ", ".join(missing) if missing else None)
            )
            async with self.session_factory() as db:
                installation = await db.get(GitHubInstallation, installation_uuid)
                app = await db.get(GitHubAppConfiguration, app_uuid)
                if installation is None or app is None:
                    continue
                installation.granted_permissions = permissions
                installation.active = not unavailable
                installation.checked_at = datetime.now(UTC)
                installation.last_error = issue
                app.connection_status = "permission_missing" if missing else (
                    "suspended" if unavailable else "ready"
                )
                app.checked_at = datetime.now(UTC)
                app.last_error = issue
                links = (
                    await db.scalars(
                        select(GitHubRepositoryLink).where(
                            GitHubRepositoryLink.installation_id == installation_uuid,
                            GitHubRepositoryLink.opted_in.is_(True),
                        )
                    )
                ).all()
                if unavailable:
                    for link in links:
                        link.setup_status = "stale"
                        link.last_error = issue
                    await db.commit()
                    reconciled += 1
                    continue
                await db.commit()
                link_snapshots = [(row.id, row.github_repository_id, row.owner, row.name) for row in links]
            for link_uuid, repository_id, owner, name in link_snapshots:
                try:
                    remote_repository = await client.request_installation(
                        app_id=app_id,
                        private_key_reference=key_ref,
                        installation_id=installation_number,
                        repository_id=repository_id,
                        method="GET",
                        path=f"/repos/{owner}/{name}",
                        permissions={"metadata": "read"},
                    )
                except GitHubAppError as exc:
                    if exc.status_code not in {403, 404}:
                        logger.warning(
                            "GitHub repository access reconciliation failed for repository %s",
                            repository_id,
                            exc_info=True,
                        )
                        continue
                    async with self.session_factory() as db:
                        link_row = await db.get(GitHubRepositoryLink, link_uuid)
                        if link_row is not None:
                            link_row.setup_status = "revoked" if exc.status_code == 404 else "stale"
                            if exc.status_code == 404:
                                link_row.opted_in = False
                            link_row.last_error = (
                                "GitHub repository access was removed"
                                if exc.status_code == 404
                                else "GitHub repository access is forbidden; setup must be refreshed"
                            )
                            await db.commit()
                    reconciled += 1
                    continue
                if not isinstance(remote_repository, dict) or remote_repository.get("id") != repository_id:
                    continue
                async with self.session_factory() as db:
                    link_row = await db.get(GitHubRepositoryLink, link_uuid)
                    if link_row is not None and link_row.opted_in:
                        link_row.setup_status = "ready"
                        link_row.last_error = None
                        link_row.last_synced_at = datetime.now(UTC)
                        await db.commit()
                reconciled += 1
        return reconciled

    async def _scope(
        self, db: AsyncSession, delivery: DeliveryEnvelope, payload: dict[str, Any]
    ) -> tuple[GitHubInstallation, GitHubRepositoryLink] | None:
        if delivery.installation_number is None or delivery.github_repository_id is None:
            return None
        installation = await db.scalar(
            select(GitHubInstallation)
            .join(
                GitHubAppConfiguration,
                GitHubAppConfiguration.id == GitHubInstallation.app_configuration_id,
            )
            .where(
                GitHubInstallation.installation_id == delivery.installation_number,
                GitHubInstallation.app_configuration_id == delivery.app_configuration_id,
                GitHubInstallation.active.is_(True),
                GitHubAppConfiguration.enabled.is_(True),
            )
        )
        if installation is None:
            return None
        link = await db.scalar(
            select(GitHubRepositoryLink).where(
                GitHubRepositoryLink.installation_id == installation.id,
                GitHubRepositoryLink.github_repository_id == delivery.github_repository_id,
                GitHubRepositoryLink.opted_in.is_(True),
                GitHubRepositoryLink.setup_status == "ready",
            )
        )
        return (installation, link) if link is not None else None

    async def _mapped_actor(
        self,
        db: AsyncSession,
        installation_id: UUID,
        sender: Any,
    ) -> tuple[User, GitHubActorMapping] | None:
        if not isinstance(sender, dict) or not isinstance(sender.get("id"), int):
            return None
        mapping = await db.scalar(
            select(GitHubActorMapping).where(
                GitHubActorMapping.installation_id == installation_id,
                GitHubActorMapping.github_user_id == sender["id"],
                GitHubActorMapping.active.is_(True),
            )
        )
        if mapping is None:
            return None
        user = await db.get(User, mapping.user_id)
        return (user, mapping) if user is not None and user.is_active else None

    async def _issue_opened(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        payload = delivery.payload
        issue = payload.get("issue")
        repository = payload.get("repository")
        if not isinstance(issue, dict) or not isinstance(repository, dict):
            return DeliveryOutcome("ignored", {"reason": "missing_issue_or_repository"})
        labels = issue.get("labels")
        if not isinstance(labels, list):
            return DeliveryOutcome("ignored", {"reason": "missing_labels"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            installation, link = scope
            if not any(
                isinstance(label, dict)
                and isinstance(label.get("name"), str)
                and label["name"].casefold() == link.topic_label.casefold()
                for label in labels
            ):
                return DeliveryOutcome("ignored", {"reason": "topic_label_missing"})
            issue_id = issue.get("id")
            issue_number = issue.get("number")
            if not isinstance(issue_id, int) or not isinstance(issue_number, int):
                return DeliveryOutcome("ignored", {"reason": "issue_identity_missing"})
            existing = await db.scalar(
                select(GitHubSyncObject).where(
                    GitHubSyncObject.repository_link_id == link.id,
                    GitHubSyncObject.object_kind == "issue",
                    GitHubSyncObject.github_object_id == str(issue_id),
                )
            )
            if existing is not None:
                await self._ensure_issue_comment(
                    db,
                    link.id,
                    existing.object_number or 0,
                    existing.task_id,
                    f"github-issue-open:{installation.installation_id}:{issue_id}",
                )
                await db.commit()
                return DeliveryOutcome(
                    "processed", {"replayed": True, "task_id": str(existing.task_id)}
                )
            actor_pair = await self._mapped_actor(db, installation.id, payload.get("sender"))
            if actor_pair is None:
                return DeliveryOutcome("ignored", {"reason": "sender_not_mapped"})
            actor, _ = actor_pair
            title = issue.get("title")
            body = issue.get("body")
            if not isinstance(title, str) or not title.strip():
                return DeliveryOutcome("ignored", {"reason": "issue_title_missing"})
            topic = title.strip()[:300]
            if isinstance(body, str) and body.strip():
                topic += "\n\n" + body.strip()[:12000]
            interview, _ = await create_interview(
                db,
                project_id=link.project_id,
                repository_id=link.repository_id,
                actor=actor,
                topic=topic,
                idempotency_key=f"github-issue:{installation.installation_id}:{issue_id}",
            )
            sync = GitHubSyncObject(
                organization_id=link.organization_id,
                project_id=link.project_id,
                repository_link_id=link.id,
                task_id=interview.task_id,
                object_kind="issue",
                github_object_id=str(issue_id),
                object_number=issue_number,
                stable_key=f"github-issue:{installation.installation_id}:{issue_id}",
                url=issue.get("html_url") if isinstance(issue.get("html_url"), str) else None,
                state="open",
                field_baseline={"title": title, "body": body if isinstance(body, str) else ""},
            )
            db.add(sync)
            link.last_synced_at = datetime.now(UTC)
            await self._ensure_issue_comment(
                db,
                link.id,
                issue_number,
                interview.task_id,
                f"github-issue-open:{installation.installation_id}:{issue_id}",
            )
            await db.commit()
            return DeliveryOutcome(
                "processed", {"task_id": str(interview.task_id), "interview_id": str(interview.id)}
            )

    async def _issue_edited(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        issue = delivery.payload.get("issue")
        repository = delivery.payload.get("repository")
        if (
            not isinstance(issue, dict)
            or not isinstance(repository, dict)
            or not isinstance(issue.get("id"), int)
        ):
            return DeliveryOutcome("ignored", {"reason": "missing_issue_identity"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, delivery.payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            _, link = scope
            row = await db.scalar(
                select(GitHubSyncObject)
                .where(
                    GitHubSyncObject.repository_link_id == link.id,
                    GitHubSyncObject.object_kind == "issue",
                    GitHubSyncObject.github_object_id == str(issue["id"]),
                )
                .with_for_update()
            )
            if row is None:
                return DeliveryOutcome("ignored", {"reason": "untracked_issue"})
            baseline = row.field_baseline
            changes: dict[str, Any] = {}
            for field in ("title", "body"):
                current = issue.get(field) if isinstance(issue.get(field), str) else ""
                if current != baseline.get(field):
                    changes[field] = {"synchronized": baseline.get(field), "remote": current}
            if changes:
                row.state = "conflict"
                row.sync_conflicts = changes
                await db.commit()
                return DeliveryOutcome("processed", {"conflict_fields": sorted(changes)})
            return DeliveryOutcome("processed", {"unchanged": True})

    async def _issue_comment(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        payload = delivery.payload
        issue, comment = payload.get("issue"), payload.get("comment")
        repository = payload.get("repository")
        if (
            not isinstance(issue, dict)
            or not isinstance(comment, dict)
            or not isinstance(repository, dict)
        ):
            return DeliveryOutcome("ignored", {"reason": "missing_comment_context"})
        body = comment.get("body")
        parsed = _ANSWER_COMMAND.fullmatch(body.strip()) if isinstance(body, str) else None
        if parsed is None or not isinstance(issue.get("id"), int):
            return DeliveryOutcome("ignored", {"reason": "not_a_structured_answer"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            installation, link = scope
            sync = await db.scalar(
                select(GitHubSyncObject).where(
                    GitHubSyncObject.repository_link_id == link.id,
                    GitHubSyncObject.object_kind == "issue",
                    GitHubSyncObject.github_object_id == str(issue["id"]),
                )
            )
            if sync is None or sync.task_id is None:
                return DeliveryOutcome("ignored", {"reason": "untracked_issue"})
            actor_pair = await self._mapped_actor(db, installation.id, payload.get("sender"))
            if actor_pair is None:
                return DeliveryOutcome("ignored", {"reason": "sender_not_mapped"})
            actor, _ = actor_pair
            interview = await db.scalar(
                select(SpecificationInterview).where(SpecificationInterview.task_id == sync.task_id)
            )
            if interview is None:
                return DeliveryOutcome("ignored", {"reason": "interview_not_found"})
            question_id = UUID(parsed.group("question"))
            question = await db.scalar(
                select(SpecificationQuestion).where(
                    SpecificationQuestion.id == question_id,
                    SpecificationQuestion.interview_id == interview.id,
                )
            )
            if question is None:
                return DeliveryOutcome("ignored", {"reason": "question_not_found"})
            try:
                answer, _ = await answer_question(
                    db,
                    interview_id=interview.id,
                    question_id=question_id,
                    actor=actor,
                    expected_revision=int(parsed.group("revision")),
                    answer=parsed.group("answer"),
                    idempotency_key=f"github-comment:{installation.installation_id}:{delivery.delivery_id}",
                    changes_scope=False,
                )
            except HTTPException as exc:
                if exc.status_code in {403, 404, 409, 422}:
                    return DeliveryOutcome("ignored", {"reason": "answer_not_authorized_or_stale"})
                raise
            await db.commit()
            await self._ensure_issue_comment(
                db,
                link.id,
                sync.object_number or 0,
                sync.task_id,
                f"github-answer:{installation.installation_id}:{delivery.delivery_id}",
                comment=f"Clarification answer recorded for question {question_id} at revision {parsed.group('revision')}.",
            )
            await db.commit()
            return DeliveryOutcome(
                "processed", {"answer_id": str(answer.id), "question_id": str(question_id)}
            )

    async def _installation_state(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        async with self.session_factory() as db:
            if delivery.installation_number is None:
                return DeliveryOutcome("ignored", {"reason": "installation_identity_missing"})
            installation = await db.scalar(
                select(GitHubInstallation)
                .where(
                    GitHubInstallation.app_configuration_id == delivery.app_configuration_id,
                    GitHubInstallation.installation_id == delivery.installation_number,
                )
                .with_for_update()
            )
            if installation is None:
                return DeliveryOutcome("ignored", {"reason": "unknown_installation"})
            if delivery.event_name == "installation" and delivery.action in {"deleted", "suspend"}:
                installation.active = False
                links = (
                    await db.scalars(
                        select(GitHubRepositoryLink)
                        .where(GitHubRepositoryLink.installation_id == installation.id)
                        .with_for_update()
                    )
                ).all()
                for link in links:
                    link.setup_status = "revoked"
                    link.last_error = "GitHub App installation was removed or suspended"
            elif delivery.event_name == "installation" and delivery.action == "unsuspend":
                installation.last_error = "Refresh installation permissions to reactivate this link"
            elif (
                delivery.event_name == "installation_repositories" and delivery.action == "removed"
            ):
                removed = delivery.payload.get("repositories_removed")
                removed_ids = (
                    {
                        item.get("id")
                        for item in removed
                        if isinstance(item, dict) and isinstance(item.get("id"), int)
                    }
                    if isinstance(removed, list)
                    else set()
                )
                links = (
                    (
                        await db.scalars(
                            select(GitHubRepositoryLink)
                            .where(
                                GitHubRepositoryLink.installation_id == installation.id,
                                GitHubRepositoryLink.github_repository_id.in_(removed_ids),
                            )
                            .with_for_update()
                        )
                    ).all()
                    if removed_ids
                    else []
                )
                for link in links:
                    link.setup_status = "revoked"
                    link.last_error = (
                        "GitHub App installation access to this repository was removed"
                    )
            await db.commit()
            return DeliveryOutcome("processed", {"installation_active": installation.active})

    async def _ensure_issue_comment(
        self,
        db: AsyncSession,
        repository_link_id: UUID,
        issue_number: int,
        task_id: UUID | None,
        stable_key: str,
        *,
        comment: str | None = None,
    ) -> None:
        existing = await db.scalar(
            select(EffectIntent).where(EffectIntent.stable_key == stable_key)
        )
        if existing is not None:
            return
        if issue_number <= 0:
            return
        link = await db.get(GitHubRepositoryLink, repository_link_id)
        if link is None:
            return
        text = comment or f"Harnessa created an interview for this issue. Task: `{task_id}`."
        db.add(
            EffectIntent(
                organization_id=link.organization_id,
                project_id=link.project_id,
                task_id=task_id,
                kind="github.issue.comment",
                stable_key=stable_key,
                payload={
                    "repository_link_id": str(repository_link_id),
                    "issue_number": issue_number,
                    "comment": text,
                    "marker": stable_key,
                },
            )
        )

    async def _pull_request_changed(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        event_pr = delivery.payload.get("pull_request")
        number = delivery.payload.get("number")
        if not isinstance(event_pr, dict) or not isinstance(number, int):
            return DeliveryOutcome("ignored", {"reason": "pull_request_identity_missing"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, delivery.payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            installation, link = scope
            sync = await db.scalar(
                select(GitHubSyncObject)
                .where(
                    GitHubSyncObject.repository_link_id == link.id,
                    GitHubSyncObject.object_kind == "pull_request",
                    GitHubSyncObject.object_number == number,
                )
                .with_for_update()
            )
            if sync is None:
                return DeliveryOutcome("ignored", {"reason": "untracked_pull_request"})
            app_config = await db.get(GitHubAppConfiguration, installation.app_configuration_id)
            if app_config is None or not app_config.enabled:
                return DeliveryOutcome("ignored", {"reason": "github_app_disabled"})
            expected_id = event_pr.get("id")
            remote = await GitHubAppClient().request_installation(
                app_id=app_config.app_id,
                private_key_reference=app_config.private_key_secret_ref,
                installation_id=installation.installation_id,
                repository_id=link.github_repository_id,
                method="GET",
                path=f"/repos/{link.owner}/{link.name}/pulls/{number}",
                permissions={"pull_requests": "read"},
            )
            if (
                not isinstance(remote, dict)
                or not isinstance(expected_id, int)
                or remote.get("id") != expected_id
                or not isinstance(remote.get("head"), dict)
                or not isinstance(remote.get("base"), dict)
            ):
                return DeliveryOutcome(
                    "ignored", {"reason": "remote_pull_request_identity_mismatch"}
                )
            head_sha = remote["head"].get("sha")
            base_sha = remote["base"].get("sha")
            if not isinstance(head_sha, str) or not isinstance(base_sha, str):
                return DeliveryOutcome("ignored", {"reason": "remote_pull_request_sha_missing"})
            baseline = sync.field_baseline
            published_sha = baseline.get("published_branch_sha")
            head_matches = isinstance(published_sha, str) and head_sha == published_sha
            published_base_sha = baseline.get("published_base_sha")
            base_matches = not isinstance(published_base_sha, str) or base_sha == published_base_sha
            state = _pull_request_state(
                merged=remote.get("merged") is True,
                remote_state=str(remote.get("state") or "unknown"),
                head_matches=head_matches,
                base_matches=base_matches,
            )
            if head_matches:
                sync.sync_conflicts = {}
            else:
                sync.sync_conflicts = {**sync.sync_conflicts, "remote_head_sha": head_sha}
            sync.state = state
            sync.head_sha = head_sha
            sync.base_sha = base_sha
            sync.reconciled_at = datetime.now(UTC)
            if isinstance(remote.get("html_url"), str):
                sync.url = remote["html_url"]
            merge_result = None
            if state == "merged":
                merge_result = await self._record_github_merge(
                    db,
                    installation=installation,
                    link=link,
                    sync=sync,
                    remote_pull_request=remote,
                    app_configuration=app_config,
                )
                if merge_result is None:
                    state = "merged_unverified"
                    sync.state = state
            elif state == "merged_head_drift":
                state = "merged_unverified"
                sync.state = state
                sync.sync_conflicts = {
                    **sync.sync_conflicts,
                    "merge_head_sha_mismatch": head_sha,
                }
            elif state in {"head_drift", "base_drift", "closed_unmerged"}:
                await self._recover_unmerged_review(
                    db,
                    sync=sync,
                    reason=(
                        "GitHub pull request was closed without merging"
                        if state == "closed_unmerged"
                        else (
                            "GitHub pull request base changed after review publication"
                            if state == "base_drift"
                            else "GitHub pull request head changed after review publication"
                        )
                    ),
                    reason_code=state,
                )
            await db.commit()
            return DeliveryOutcome(
                "processed",
                {
                    "state": state,
                    "head_sha": head_sha,
                    "base_sha": base_sha,
                    "integration_id": str(merge_result) if merge_result else None,
                },
            )

    async def _recover_unmerged_review(
        self,
        db: AsyncSession,
        *,
        sync: GitHubSyncObject,
        reason: str,
        reason_code: str,
    ) -> None:
        """Invalidate stale review evidence and return bounded work to READY."""
        if sync.task_id is None:
            return
        task = await db.scalar(select(Task).where(Task.id == sync.task_id).with_for_update())
        if task is None or task.stage != "HUMAN_REVIEW":
            return
        marker = f"github-pr-recovery:{sync.id}:{sync.head_sha}:{reason_code}"
        if sync.sync_conflicts.get("recovery_marker") == marker:
            return
        run_count = int(
            await db.scalar(select(func.count()).select_from(Run).where(Run.task_id == task.id))
            or 0
        )
        if run_count >= settings.run_max_attempts:
            sync.sync_conflicts = {
                **sync.sync_conflicts,
                "recovery_marker": marker,
                "recovery_cap_reached": reason_code,
            }
            return
        baseline = sync.field_baseline
        bundle_id_value = baseline.get("bundle_id")
        if isinstance(bundle_id_value, str):
            bundle = await db.get(ReviewBundle, UUID(bundle_id_value))
            if bundle is not None and bundle.status == "published":
                bundle.status = "invalidated"
                bundle.invalidated_reason = reason[:500]
        sync.sync_conflicts = {**sync.sync_conflicts, "recovery_marker": marker}
        before = task.version
        prior_status = task.run_status
        task.stage = "READY"
        task.run_status = "QUEUED"
        task.blocked_reason = f"github_{reason_code}"
        task.version += 1
        db.add(
            TaskTransition(
                task_id=task.id,
                project_id=task.project_id,
                actor_id=None,
                from_stage="HUMAN_REVIEW",
                to_stage="READY",
                from_run_status=prior_status,
                to_run_status="QUEUED",
                reason=f"{reason}; bounded remediation queued",
                command="github_pr_recovered",
                command_key=marker,
                version_before=before,
                version_after=task.version,
            )
        )

    async def _record_github_merge(
        self,
        db: AsyncSession,
        *,
        installation: GitHubInstallation,
        link: GitHubRepositoryLink,
        sync: GitHubSyncObject,
        remote_pull_request: dict[str, Any],
        app_configuration: GitHubAppConfiguration,
    ) -> UUID | None:
        if sync.task_id is None:
            return None
        baseline = sync.field_baseline
        bundle_id_value = baseline.get("bundle_id")
        published_sha = baseline.get("published_branch_sha")
        head = remote_pull_request.get("head")
        merge_sha = remote_pull_request.get("merge_commit_sha")
        remote_id = remote_pull_request.get("id")
        pr_number = remote_pull_request.get("number")
        if (
            not isinstance(bundle_id_value, str)
            or not isinstance(published_sha, str)
            or not isinstance(head, dict)
            or head.get("sha") != published_sha
            or not isinstance(merge_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", merge_sha)
            or not isinstance(remote_id, int)
            or not isinstance(pr_number, int)
        ):
            return None
        client = GitHubAppClient()
        current_checks = await client.request_installation(
            app_id=app_configuration.app_id,
            private_key_reference=app_configuration.private_key_secret_ref,
            installation_id=installation.installation_id,
            repository_id=link.github_repository_id,
            method="GET",
            path=f"/repos/{link.owner}/{link.name}/commits/{published_sha}/check-runs",
            params={"per_page": 100},
            permissions={"checks": "read"},
        )
        run_rows = current_checks.get("check_runs") if isinstance(current_checks, dict) else None
        if not isinstance(run_rows, list):
            return None
        actual_checks = {
            row["name"]: {"status": row.get("status"), "conclusion": row.get("conclusion"), "id": row.get("id")}
            for row in run_rows
            if isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and row.get("head_sha") == published_sha
        }
        required_checks = {
            name: actual_checks.get(name, {"status": "missing", "conclusion": None})
            for name in link.required_checks
        }
        all_required_passed = all(
            item.get("status") == "completed" and item.get("conclusion") == "success"
            for item in required_checks.values()
        )
        if not all_required_passed:
            return None
        bundle = await db.get(ReviewBundle, UUID(bundle_id_value))
        if bundle is None or bundle.status != "published" or bundle.task_id != sync.task_id:
            return None
        remote_reviews = await client.request_installation(
            app_id=app_configuration.app_id,
            private_key_reference=app_configuration.private_key_secret_ref,
            installation_id=installation.installation_id,
            repository_id=link.github_repository_id,
            method="GET",
            path=f"/repos/{link.owner}/{link.name}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
            permissions={"pull_requests": "read"},
        )
        approval = None
        if isinstance(remote_reviews, list):
            github_user_ids = {
                review["user"]["id"]
                for review in remote_reviews
                if isinstance(review, dict)
                and isinstance(review.get("user"), dict)
                and isinstance(review["user"].get("id"), int)
                and review.get("commit_id") == published_sha
            }
            mappings = (
                await db.scalars(
                    select(GitHubActorMapping).where(
                        GitHubActorMapping.installation_id == installation.id,
                        GitHubActorMapping.github_user_id.in_(github_user_ids),
                        GitHubActorMapping.active.is_(True),
                    )
                )
            ).all() if github_user_ids else []
            actor_by_github_id = {
                row.github_user_id: row.user_id for row in mappings
            }
            latest_by_actor = _latest_review_state_by_actor(
                remote_reviews, actor_by_github_id, published_sha
            )
            authorized_actor_ids: set[UUID] = set()
            for actor_id in latest_by_actor:
                user = await db.get(User, actor_id)
                if user is None or not user.is_active:
                    continue
                org_membership = await db.scalar(
                    select(Membership).where(
                        Membership.organization_id == link.organization_id,
                        Membership.user_id == user.id,
                    )
                )
                project_membership = await db.scalar(
                    select(ProjectMembership).where(
                        ProjectMembership.project_id == link.project_id,
                        ProjectMembership.user_id == user.id,
                    )
                )
                if not (
                    (org_membership is not None and org_membership.role == "administrator")
                    or (project_membership is not None and project_membership.role == "reviewer")
                ):
                    continue
                authorized_actor_ids.add(actor_id)
            authorized_latest_by_actor = _authorized_latest_review_states(
                latest_by_actor, authorized_actor_ids
            )
            if any(
                str(review.get("state", "")).casefold() == "changes_requested"
                for review in authorized_latest_by_actor.values()
            ):
                return None
            for actor_id, remote_review in authorized_latest_by_actor.items():
                if str(remote_review.get("state", "")).casefold() != "approved":
                    continue
                approval = await db.scalar(
                    select(ReviewDecision)
                    .where(
                        ReviewDecision.actor_id == actor_id,
                        ReviewDecision.bundle_id == bundle.id,
                        ReviewDecision.decision == "approve",
                        ReviewDecision.github_head_sha == published_sha,
                    )
                    .order_by(ReviewDecision.created_at.desc())
                    .limit(1)
                )
                if approval is not None:
                    break
        if approval is None:
            return None
        verification = await db.get(VerificationExecution, bundle.verification_execution_id)
        if (
            verification is None
            or verification.outcome != "passed"
            or any(item.get("outcome") != "passed" for item in verification.result.get("criteria", []))
        ):
            return None
        task = await db.scalar(select(Task).where(Task.id == sync.task_id).with_for_update())
        if task is None or task.stage not in {"HUMAN_REVIEW", "LEARNING"}:
            return None
        stable_key = f"github-pr-merge:{link.github_repository_id}:{remote_id}:{merge_sha}"
        existing = await db.scalar(
            select(IntegrationEvidence)
            .where(
                IntegrationEvidence.project_id == link.project_id,
                IntegrationEvidence.stable_key == stable_key,
            )
            .with_for_update()
        )
        if existing is not None:
            return existing.id
        evidence = {
            "provider": "github",
            "installation_id": installation.installation_id,
            "repository_id": link.github_repository_id,
            "pull_request_id": remote_id,
            "pull_request_number": pr_number,
            "pull_request_url": remote_pull_request.get("html_url"),
            "review_bundle_id": str(bundle.id),
            "bundle_digest": bundle.bundle_digest,
            "verified_source_head_sha": bundle.head_sha,
            "github_head_sha": published_sha,
            "base_sha": bundle.base_sha,
            "merge_commit_sha": merge_sha,
            "review_decision_id": str(approval.id),
            "required_checks": required_checks,
        }
        evidence_digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        integration = IntegrationEvidence(
            organization_id=link.organization_id,
            project_id=link.project_id,
            repository_id=link.repository_id,
            task_id=task.id,
            bundle_id=bundle.id,
            mode="github",
            stable_key=stable_key,
            status="confirmed",
            evidence=evidence,
            evidence_digest=evidence_digest,
            integration_revision=merge_sha,
            confirmed_by=None,
            confirmed_at=datetime.now(UTC),
        )
        db.add(integration)
        await db.flush()
        for kind in ("archive", "learning", "cleanup"):
            db.add(
                PostIntegrationStep(
                    integration_evidence_id=integration.id,
                    kind=kind,
                    status="pending",
                    result={},
                )
            )
        if task.stage == "HUMAN_REVIEW":
            before = task.version
            prior_status = task.run_status
            task.stage = "LEARNING"
            task.version += 1
            db.add(
                TaskTransition(
                    task_id=task.id,
                    project_id=task.project_id,
                    actor_id=None,
                    from_stage="HUMAN_REVIEW",
                    to_stage="LEARNING",
                    from_run_status=prior_status,
                    to_run_status=prior_status,
                    reason="GitHub merge was verified; durable archive, learning, and cleanup steps are pending",
                    command="github_integration_confirmed",
                    command_key=stable_key,
                    version_before=before,
                    version_after=task.version,
                )
            )
        return integration.id

    async def _checks_completed(
        self, delivery: DeliveryEnvelope, event_kind: str
    ) -> DeliveryOutcome:
        record = delivery.payload.get(event_kind)
        if not isinstance(record, dict):
            return DeliveryOutcome("ignored", {"reason": "check_identity_missing"})
        head_sha = record.get("head_sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head_sha):
            return DeliveryOutcome("ignored", {"reason": "check_head_sha_invalid"})
        associated = record.get("pull_requests")
        if not isinstance(associated, list) or not associated:
            return DeliveryOutcome("ignored", {"reason": "check_has_no_pull_request"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, delivery.payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            installation, link = scope
            app_config = await db.get(GitHubAppConfiguration, installation.app_configuration_id)
            if app_config is None or not app_config.enabled:
                return DeliveryOutcome("ignored", {"reason": "github_app_disabled"})
            persisted: list[dict[str, Any]] = []
            for associated_pr in associated[:20]:
                if not isinstance(associated_pr, dict) or not isinstance(
                    associated_pr.get("number"), int
                ):
                    continue
                number = associated_pr["number"]
                sync = await db.scalar(
                    select(GitHubSyncObject)
                    .where(
                        GitHubSyncObject.repository_link_id == link.id,
                        GitHubSyncObject.object_kind == "pull_request",
                        GitHubSyncObject.object_number == number,
                    )
                    .with_for_update()
                )
                if sync is None:
                    continue
                remote = await GitHubAppClient().request_installation(
                    app_id=app_config.app_id,
                    private_key_reference=app_config.private_key_secret_ref,
                    installation_id=installation.installation_id,
                    repository_id=link.github_repository_id,
                    method="GET",
                    path=f"/repos/{link.owner}/{link.name}/pulls/{number}",
                    permissions={"pull_requests": "read"},
                )
                if (
                    not isinstance(remote, dict)
                    or remote.get("id") != associated_pr.get("id")
                    or not isinstance(remote.get("head"), dict)
                    or remote["head"].get("sha") != head_sha
                ):
                    continue
                remote_checks = await GitHubAppClient().request_installation(
                    app_id=app_config.app_id,
                    private_key_reference=app_config.private_key_secret_ref,
                    installation_id=installation.installation_id,
                    repository_id=link.github_repository_id,
                    method="GET",
                    path=f"/repos/{link.owner}/{link.name}/commits/{head_sha}/check-runs",
                    params={"per_page": 100},
                    permissions={"checks": "read"},
                )
                runs = remote_checks.get("check_runs") if isinstance(remote_checks, dict) else None
                if not isinstance(runs, list):
                    continue
                current: dict[str, Any] = {}
                for run in runs:
                    if (
                        isinstance(run, dict)
                        and isinstance(run.get("name"), str)
                        and isinstance(run.get("head_sha"), str)
                        and run["head_sha"] == head_sha
                    ):
                        current[run["name"]] = {
                            "status": run.get("status"),
                            "conclusion": run.get("conclusion"),
                            "id": run.get("id"),
                        }
                required = {
                    name: current.get(name, {"status": "missing", "conclusion": None})
                    for name in link.required_checks
                }
                sync.check_results = {
                    "head_sha": head_sha,
                    "required": required,
                    "all_required_passed": all(
                        value.get("status") == "completed" and value.get("conclusion") == "success"
                        for value in required.values()
                    ),
                }
                sync.head_sha = head_sha
                all_required_complete = all(
                    value.get("status") == "completed" for value in required.values()
                )
                all_required_passed = sync.check_results["all_required_passed"] is True
                if all_required_complete and not all_required_passed and sync.task_id is not None:
                    baseline = sync.field_baseline
                    bundle_id_value = baseline.get("bundle_id")
                    task = await db.scalar(
                        select(Task).where(Task.id == sync.task_id).with_for_update()
                    )
                    already_retried = sync.sync_conflicts.get("check_remediated_head_sha") == head_sha
                    if task is not None and task.stage == "HUMAN_REVIEW" and not already_retried:
                        run_count = int(
                            await db.scalar(
                                select(func.count()).select_from(Run).where(Run.task_id == task.id)
                            )
                            or 0
                        )
                        if run_count < settings.run_max_attempts:
                            before = task.version
                            prior_status = task.run_status
                            task.stage = "READY"
                            task.run_status = "QUEUED"
                            task.blocked_reason = "github_required_check_failed"
                            task.version += 1
                            if isinstance(bundle_id_value, str):
                                failed_bundle = await db.get(ReviewBundle, UUID(bundle_id_value))
                                if failed_bundle is not None:
                                    failed_bundle.status = "invalidated"
                                    failed_bundle.invalidated_reason = "A required GitHub check failed on the published head"
                            db.add(
                                TaskTransition(
                                    task_id=task.id,
                                    project_id=task.project_id,
                                    actor_id=None,
                                    from_stage="HUMAN_REVIEW",
                                    to_stage="READY",
                                    from_run_status=prior_status,
                                    to_run_status="QUEUED",
                                    reason="A required GitHub check failed on the current published head; bounded remediation queued",
                                    command="github_check_failed",
                                    command_key=f"github-check-remediation:{task.id}:{head_sha}",
                                    version_before=before,
                                    version_after=task.version,
                                )
                            )
                            sync.sync_conflicts = {
                                **sync.sync_conflicts,
                                "check_remediated_head_sha": head_sha,
                            }
                        else:
                            sync.sync_conflicts = {
                                **sync.sync_conflicts,
                                "check_remediation_cap_reached": head_sha,
                            }
                persisted.append(
                    {
                        "pull_number": number,
                        "all_required_passed": sync.check_results["all_required_passed"],
                    }
                )
            await db.commit()
            if not persisted:
                return DeliveryOutcome("ignored", {"reason": "stale_or_untracked_check_head"})
            return DeliveryOutcome("processed", {"checks": persisted})

    async def _review_changed(self, delivery: DeliveryEnvelope) -> DeliveryOutcome:
        event_pr = delivery.payload.get("pull_request")
        review_event = delivery.payload.get("review")
        number = delivery.payload.get("number")
        if (
            not isinstance(event_pr, dict)
            or not isinstance(review_event, dict)
            or not isinstance(number, int)
            or not isinstance(review_event.get("id"), int)
        ):
            return DeliveryOutcome("ignored", {"reason": "review_identity_missing"})
        async with self.session_factory() as db:
            scope = await self._scope(db, delivery, delivery.payload)
            if scope is None:
                return DeliveryOutcome("ignored", {"reason": "repository_not_opted_in"})
            installation, link = scope
            actor_pair = await self._mapped_actor(
                db, installation.id, delivery.payload.get("sender")
            )
            if actor_pair is None:
                return DeliveryOutcome("ignored", {"reason": "reviewer_not_mapped"})
            reviewer, _ = actor_pair
            org_membership = await db.scalar(
                select(Membership).where(
                    Membership.organization_id == link.organization_id,
                    Membership.user_id == reviewer.id,
                )
            )
            project_membership = await db.scalar(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == link.project_id,
                    ProjectMembership.user_id == reviewer.id,
                )
            )
            is_admin = org_membership is not None and org_membership.role == "administrator"
            is_reviewer = project_membership is not None and project_membership.role == "reviewer"
            if not (is_admin or is_reviewer):
                return DeliveryOutcome("ignored", {"reason": "reviewer_lacks_review_permission"})
            sync = await db.scalar(
                select(GitHubSyncObject)
                .where(
                    GitHubSyncObject.repository_link_id == link.id,
                    GitHubSyncObject.object_kind == "pull_request",
                    GitHubSyncObject.object_number == number,
                )
                .with_for_update()
            )
            if sync is None or sync.task_id is None:
                return DeliveryOutcome("ignored", {"reason": "untracked_pull_request"})
            baseline = sync.field_baseline
            bundle_id_value = baseline.get("bundle_id")
            published_sha = baseline.get("published_branch_sha")
            if not isinstance(bundle_id_value, str) or not isinstance(published_sha, str):
                return DeliveryOutcome("ignored", {"reason": "review_bundle_binding_missing"})
            bundle = await db.get(ReviewBundle, UUID(bundle_id_value))
            task = await db.scalar(select(Task).where(Task.id == sync.task_id).with_for_update())
            app_config = await db.get(GitHubAppConfiguration, installation.app_configuration_id)
            if bundle is None or task is None or app_config is None or not app_config.enabled:
                return DeliveryOutcome("ignored", {"reason": "review_bundle_or_task_missing"})
            client = GitHubAppClient()
            remote_pr = await client.request_installation(
                app_id=app_config.app_id,
                private_key_reference=app_config.private_key_secret_ref,
                installation_id=installation.installation_id,
                repository_id=link.github_repository_id,
                method="GET",
                path=f"/repos/{link.owner}/{link.name}/pulls/{number}",
                permissions={"pull_requests": "read"},
            )
            remote_reviews = await client.request_installation(
                app_id=app_config.app_id,
                private_key_reference=app_config.private_key_secret_ref,
                installation_id=installation.installation_id,
                repository_id=link.github_repository_id,
                method="GET",
                path=f"/repos/{link.owner}/{link.name}/pulls/{number}/reviews",
                params={"per_page": 100},
                permissions={"pull_requests": "read"},
            )
            review_id = review_event["id"]
            current_review = (
                next(
                    (
                        item
                        for item in remote_reviews
                        if isinstance(item, dict) and item.get("id") == review_id
                    ),
                    None,
                )
                if isinstance(remote_reviews, list)
                else None
            )
            if (
                not isinstance(remote_pr, dict)
                or remote_pr.get("id") != event_pr.get("id")
                or not isinstance(remote_pr.get("head"), dict)
                or remote_pr["head"].get("sha") != published_sha
                or current_review is None
                or current_review.get("commit_id") != published_sha
            ):
                return DeliveryOutcome("ignored", {"reason": "stale_or_unverified_review"})
            remote_state = str(current_review.get("state", "")).casefold()
            if delivery.action == "dismissed" or remote_state == "dismissed":
                decision = "comment"
            elif remote_state == "approved":
                decision = "approve"
            elif remote_state == "changes_requested":
                decision = "request_changes"
            else:
                decision = "comment"
            idempotency_key = (
                f"github-review:{installation.installation_id}:{review_id}:{delivery.action}"
            )
            prior = await db.scalar(
                select(ReviewDecision).where(
                    ReviewDecision.actor_id == reviewer.id,
                    ReviewDecision.idempotency_key == idempotency_key,
                )
            )
            if prior is not None:
                return DeliveryOutcome("processed", {"replayed": True, "decision": prior.decision})
            body = current_review.get("body")
            rationale = body[:2000] if isinstance(body, str) else ""
            db.add(
                ReviewDecision(
                    organization_id=link.organization_id,
                    project_id=link.project_id,
                    task_id=task.id,
                    bundle_id=bundle.id,
                    actor_id=reviewer.id,
                    decision=decision,
                    rationale=rationale,
                    idempotency_key=idempotency_key,
                    bundle_digest=bundle.bundle_digest,
                    head_sha=bundle.head_sha,
                    github_head_sha=published_sha,
                )
            )
            if decision == "approve" and task.stage == "HUMAN_REVIEW":
                bundle.status = "published"
            elif decision == "request_changes" and task.stage == "HUMAN_REVIEW":
                run_count = int(
                    await db.scalar(
                        select(func.count()).select_from(Run).where(Run.task_id == task.id)
                    )
                    or 0
                )
                if run_count < settings.run_max_attempts:
                    before = task.version
                    prior_status = task.run_status
                    task.stage = "READY"
                    task.run_status = "QUEUED"
                    task.blocked_reason = "github_review_changes_requested"
                    task.version += 1
                    bundle.status = "invalidated"
                    bundle.invalidated_reason = "GitHub reviewer requested changes"
                    db.add(
                        TaskTransition(
                            task_id=task.id,
                            project_id=task.project_id,
                            actor_id=reviewer.id,
                            from_stage="HUMAN_REVIEW",
                            to_stage="READY",
                            from_run_status=prior_status,
                            to_run_status="QUEUED",
                            reason="Mapped GitHub reviewer requested changes on the current published head",
                            command="github_review_changes_requested",
                            command_key=idempotency_key,
                            version_before=before,
                            version_after=task.version,
                        )
                    )
                else:
                    sync.sync_conflicts = {
                        **sync.sync_conflicts,
                        "review_remediation_cap_reached": published_sha,
                    }
            await db.commit()
            return DeliveryOutcome(
                "processed", {"decision": decision, "github_head_sha": published_sha}
            )


def _latest_review_state_by_actor(
    reviews: list[Any], actor_by_github_id: dict[int, UUID], expected_head_sha: str
) -> dict[UUID, dict[str, Any]]:
    """Return each mapped actor's latest review state for the exact PR head."""
    ordered = sorted(
        (
            review
            for review in reviews
            if isinstance(review, dict)
            and review.get("commit_id") == expected_head_sha
            and isinstance(review.get("user"), dict)
            and isinstance(review["user"].get("id"), int)
            and review["user"]["id"] in actor_by_github_id
        ),
        key=lambda review: (
            str(review.get("submitted_at") or review.get("created_at") or ""),
            int(review.get("id", 0)) if isinstance(review.get("id"), int) else 0,
        ),
    )
    latest: dict[UUID, dict[str, Any]] = {}
    for review in ordered:
        state = str(review.get("state", "")).casefold()
        if state not in {"approved", "changes_requested", "dismissed"}:
            continue
        github_user_id = review["user"]["id"]
        latest[actor_by_github_id[github_user_id]] = review
    return latest


def _authorized_latest_review_states(
    latest_by_actor: dict[UUID, dict[str, Any]], authorized_actor_ids: set[UUID]
) -> dict[UUID, dict[str, Any]]:
    """Keep only current review dispositions from authorized local reviewers."""
    return {
        actor_id: review
        for actor_id, review in latest_by_actor.items()
        if actor_id in authorized_actor_ids
    }


def _pull_request_state(
    *, merged: bool, remote_state: str, head_matches: bool, base_matches: bool
) -> str:
    if merged:
        return "merged" if head_matches else "merged_head_drift"
    if remote_state == "closed":
        return "closed_unmerged"
    if not head_matches:
        return "head_drift"
    if not base_matches:
        return "base_drift"
    return "open"
