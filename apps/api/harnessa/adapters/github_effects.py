"""Reconciled publication of immutable review bundles to a scoped GitHub repository."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harnessa.adapters.github_app import GitHubAppClient, GitHubAppError
from harnessa.adapters.specification_workspace import SpecificationWorkspaceManager
from harnessa.db.github import (
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
)
from harnessa.db.models import Repository, Task
from harnessa.db.review import ReviewBundle
from harnessa.db.session import SessionLocal
from harnessa.services.effects import EffectResult, ReconcileOutcome

_PUBLISH_KIND = "github.review_bundle.publish"
_MARKER_PREFIX = "Harnessa-Bundle:"
_ISSUE_CREATE_KIND = "github.issue.create"
_STATUS_KIND = "github.commit_status.publish"


class GitHubEffectReconciler:
    """Publish/reconcile a branch and PR from an exact locally verified Git revision."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        *,
        workspace_manager: SpecificationWorkspaceManager | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.workspace_manager = workspace_manager or SpecificationWorkspaceManager()

    async def publish_archive_maintenance(self, integration_id: UUID) -> dict[str, Any]:
        """Publish archive-only changes as a durable PR on the current protected base."""
        from harnessa.db.review import IntegrationEvidence

        async with self.session_factory() as db:
            integration = await db.get(IntegrationEvidence, integration_id)
            if integration is None or integration.status != "confirmed":
                raise GitHubAppError("Confirmed integration evidence is unavailable")
            bundle = await db.get(ReviewBundle, integration.bundle_id)
            repository = await db.get(Repository, integration.repository_id)
            if bundle is None or repository is None:
                raise GitHubAppError("Archive maintenance source is unavailable")
            link = await db.scalar(
                select(GitHubRepositoryLink).where(
                    GitHubRepositoryLink.repository_id == integration.repository_id,
                    GitHubRepositoryLink.opted_in.is_(True),
                    GitHubRepositoryLink.setup_status == "ready",
                )
            )
            if link is None or link.archive_policy != "post_merge":
                raise GitHubAppError("Post-merge archive policy is not enabled")
            installation = await db.get(GitHubInstallation, link.installation_id)
            app = (
                await db.get(GitHubAppConfiguration, installation.app_configuration_id)
                if installation
                else None
            )
            preview = bundle.bundle.get("openspec_sync_preview")
            if (
                installation is None
                or not installation.active
                or app is None
                or not app.enabled
                or not isinstance(preview, dict)
            ):
                raise GitHubAppError("Archive maintenance installation or preview is unavailable")
            archive_diff = preview.get("archive_diff")
            archive_digest = preview.get("diff_sha256")
            if (
                not isinstance(archive_diff, str)
                or not isinstance(archive_digest, str)
                or hashlib.sha256(archive_diff.encode()).hexdigest() != archive_digest
            ):
                raise GitHubAppError("Archive maintenance diff failed its digest check")
            changes = _archive_patch_changes(archive_diff)
            source_path = await self.workspace_manager.source_path(repository)
            app_id = app.app_id
            private_key_ref = app.private_key_secret_ref
            installation_number = installation.installation_id
            github_repository_id = link.github_repository_id
            owner, name = link.owner, link.name
            organization_id, project_id = integration.organization_id, integration.project_id
            task_id = integration.task_id
            bundle_digest = bundle.bundle_digest
            merge_sha = integration.integration_revision
            stable_key = f"github-archive-pr:{integration.id}:{archive_digest}"

        if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", merge_sha):
            raise GitHubAppError("Verified integration revision is invalid")
        if not await self.workspace_manager.contains_revision(repository, bundle.head_sha):
            raise GitHubAppError("Archive candidate revision is unavailable locally")

        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[github_repository_id],
            permissions={"contents": "write", "pull_requests": "write"},
        )

        async def request(
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            params: dict[str, str | int] | None = None,
        ) -> dict[str, Any] | list[Any]:
            return await client._request(method, path, token=token, body=body, params=params)

        repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        remote_repo = await request("GET", repo_path)
        if not isinstance(remote_repo, dict) or not isinstance(
            remote_repo.get("default_branch"), str
        ):
            raise GitHubAppError("GitHub repository omitted its default branch")
        base_branch = remote_repo["default_branch"]
        base_ref = await request("GET", f"{repo_path}/git/ref/heads/{quote(base_branch, safe='')}")
        base_sha = _ref_sha(_as_dict(base_ref))
        if base_sha is None:
            raise GitHubAppError("GitHub default branch has no current commit")
        comparison = await request("GET", f"{repo_path}/compare/{merge_sha}...{base_sha}")
        merge_base = comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
        if (
            not isinstance(comparison, dict)
            or comparison.get("status") not in {"ahead", "identical"}
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != merge_sha
        ):
            raise GitHubAppError("Current default branch does not descend from the verified merge")

        branch = f"harnessa/archive-{integration_id.hex[:16]}-{archive_digest[:10]}"
        marker = f"<!-- Harnessa-Archive: {integration_id} {archive_digest} -->"
        existing_prs = await request(
            "GET",
            f"{repo_path}/pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "base": base_branch},
        )
        pr = None
        if isinstance(existing_prs, list):
            pr = next(
                (
                    item
                    for item in existing_prs
                    if isinstance(item, dict)
                    and isinstance(item.get("body"), str)
                    and marker in item["body"]
                ),
                None,
            )

        branch_ref_path = f"{repo_path}/git/ref/heads/{quote(branch, safe='')}"
        branch_ref: dict[str, Any] | None = None
        try:
            branch_ref = _as_dict(await request("GET", branch_ref_path))
        except GitHubAppError as exc:
            if exc.status_code != 404:
                raise
        if branch_ref is None:
            base_commit = await request("GET", f"{repo_path}/git/commits/{base_sha}")
            if not isinstance(base_commit, dict) or not isinstance(base_commit.get("tree"), dict):
                raise GitHubAppError("GitHub base commit did not include its tree")
            entries: list[dict[str, Any]] = []
            for path, deleted in sorted(changes.items()):
                entry: dict[str, Any] = {"path": path, "mode": "100644", "type": "blob"}
                if deleted:
                    entry["sha"] = None
                else:
                    content = await asyncio.to_thread(
                        subprocess.run,
                        ["git", "cat-file", "blob", f"{bundle.head_sha}:{path}"],
                        cwd=source_path,
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    blob = await request(
                        "POST",
                        f"{repo_path}/git/blobs",
                        {
                            "content": base64.b64encode(content.stdout).decode("ascii"),
                            "encoding": "base64",
                        },
                    )
                    if not isinstance(blob, dict) or not isinstance(blob.get("sha"), str):
                        raise GitHubAppError("GitHub did not return an archive blob")
                    entry["sha"] = blob["sha"]
                entries.append(entry)
            tree = await request(
                "POST",
                f"{repo_path}/git/trees",
                {"base_tree": base_commit["tree"].get("sha"), "tree": entries},
            )
            if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
                raise GitHubAppError("GitHub did not return an archive tree")
            commit = await request(
                "POST",
                f"{repo_path}/git/commits",
                {
                    "message": f"Archive Harnessa change {integration_id}\n\n{marker}",
                    "tree": tree["sha"],
                    "parents": [base_sha],
                },
            )
            if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
                raise GitHubAppError("GitHub did not return an archive commit")
            try:
                branch_ref = _as_dict(
                    await request(
                        "POST",
                        f"{repo_path}/git/refs",
                        {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
                    )
                )
            except GitHubAppError as exc:
                if exc.status_code != 422:
                    raise
                branch_ref = _as_dict(await request("GET", branch_ref_path))
        branch_sha = _ref_sha(branch_ref)
        if branch_sha is None:
            raise GitHubAppError("GitHub archive branch revision is unavailable")
        branch_commit = await request("GET", f"{repo_path}/git/commits/{branch_sha}")
        commit_message = branch_commit.get("message") if isinstance(branch_commit, dict) else None
        if not isinstance(commit_message, str) or marker not in commit_message:
            raise GitHubAppError("Existing archive branch is not owned by this integration")
        if pr is None:
            created_pr = await request(
                "POST",
                f"{repo_path}/pulls",
                {
                    "title": f"Harnessa archive: {bundle_digest[:12]}",
                    "head": branch,
                    "base": base_branch,
                    "body": f"Post-merge OpenSpec archive for task `{task_id}`.\n\n"
                    f"Integration: `{integration_id}`\nArchive digest: `{archive_digest}`\n\n{marker}",
                    "maintainer_can_modify": False,
                },
            )
            pr = _as_dict(created_pr)
        elif pr.get("state") == "closed" and pr.get("merged") is not True:
            reopened = await request(
                "PATCH", f"{repo_path}/pulls/{pr.get('number')}", {"state": "open"}
            )
            pr = _as_dict(reopened)
        if (
            not isinstance(pr, dict)
            or not isinstance(pr.get("id"), int)
            or not isinstance(pr.get("number"), int)
        ):
            raise GitHubAppError("GitHub did not return the archive pull request identity")
        remote_head = pr.get("head")
        if not isinstance(remote_head, dict) or remote_head.get("sha") != branch_sha:
            raise GitHubAppError("Archive pull request head differs from the owned branch")
        async with self.session_factory() as db:
            existing = await db.scalar(
                select(GitHubSyncObject)
                .where(GitHubSyncObject.stable_key == stable_key)
                .with_for_update()
            )
            if existing is None:
                existing = GitHubSyncObject(
                    organization_id=organization_id,
                    project_id=project_id,
                    repository_link_id=link.id,
                    task_id=task_id,
                    object_kind="pull_request",
                    object_purpose="maintenance_archive",
                    github_object_id=str(pr["id"]),
                    object_number=pr["number"],
                    stable_key=stable_key,
                    url=pr.get("html_url") if isinstance(pr.get("html_url"), str) else None,
                    state="awaiting_archive_merge",
                    base_sha=base_sha,
                    head_sha=branch_sha,
                    check_results={},
                    field_baseline={
                        "integration_evidence_id": str(integration_id),
                        "archive_diff_sha256": archive_digest,
                        "published_branch": branch,
                        "published_branch_sha": branch_sha,
                        "expected_base_sha": base_sha,
                    },
                    sync_conflicts={},
                )
                db.add(existing)
            else:
                existing.github_object_id = str(pr["id"])
                existing.object_number = pr["number"]
                existing.url = pr.get("html_url") if isinstance(pr.get("html_url"), str) else None
                existing.state = "awaiting_archive_merge"
                existing.base_sha = base_sha
                existing.head_sha = branch_sha
                existing.field_baseline = {
                    **existing.field_baseline,
                    "integration_evidence_id": str(integration_id),
                    "archive_diff_sha256": archive_digest,
                    "published_branch": branch,
                    "published_branch_sha": branch_sha,
                    "expected_base_sha": base_sha,
                }
            await db.commit()
        return {
            "outcome": "awaiting_merge",
            "policy": "post_merge_maintenance_pr",
            "pull_request_id": pr["id"],
            "pull_request_number": pr["number"],
            "pull_request_url": pr.get("html_url"),
            "archive_diff_sha256": archive_digest,
            "published_branch_sha": branch_sha,
            "base_sha": base_sha,
        }

    async def reconcile(
        self, *, kind: str, stable_key: str, payload: Mapping[str, object]
    ) -> EffectResult | ReconcileOutcome:
        if kind == _ISSUE_CREATE_KIND:
            return await self._reconcile_issue_create(stable_key, payload)
        if kind == "github.issue.comment":
            return await self._reconcile_issue_comment(stable_key, payload)
        if kind == _STATUS_KIND:
            return await self._reconcile_commit_status(stable_key, payload)
        if kind != _PUBLISH_KIND:
            return "unknown"
        try:
            bundle_id = UUID(str(payload["bundle_id"]))
            link_id = UUID(str(payload["repository_link_id"]))
        except (KeyError, ValueError):
            raise GitHubAppError("GitHub publication intent is invalid") from None
        async with self.session_factory() as db:
            bundle = await db.get(ReviewBundle, bundle_id)
            link = await db.get(GitHubRepositoryLink, link_id)
            if bundle is None or link is None or not link.opted_in or link.setup_status != "ready":
                return EffectResult(
                    "applied",
                    {"state": "rejected", "reason": "bundle_or_repository_not_publishable"},
                )
            if bundle.repository_id != link.repository_id or bundle.status != "prepared":
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "bundle_or_repository_changed"}
                )
            from harnessa.api.review_routes import _bundle_stale_reason

            stale_reason = await _bundle_stale_reason(db, bundle)
            if stale_reason:
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "review_bundle_became_stale"}
                )
            repository = await db.get(Repository, bundle.repository_id)
            installation = await db.get(GitHubInstallation, link.installation_id)
            app_config = (
                await db.get(GitHubAppConfiguration, installation.app_configuration_id)
                if installation
                else None
            )
            if (
                repository is None
                or installation is None
                or app_config is None
                or not installation.active
                or not app_config.enabled
            ):
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "installation_unavailable"}
                )
            source_path = await self.workspace_manager.source_path(repository)
            if not await self.workspace_manager.contains_revision(repository, bundle.head_sha):
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "review_head_not_available_locally"}
                )
            app_id = app_config.app_id
            private_key_ref = app_config.private_key_secret_ref
            installation_number = installation.installation_id
            github_repository_id = link.github_repository_id
            owner, name = link.owner, link.name
            bundle_digest, base_sha, head_sha = (
                bundle.bundle_digest,
                bundle.base_sha,
                bundle.head_sha,
            )
            task_id = bundle.task_id
            bundle_id = bundle.id
            organization_id = bundle.organization_id
            project_id = bundle.project_id
            repository_link_id = link.id
            archive_policy = link.archive_policy
            preview = bundle.bundle.get("openspec_sync_preview")
            archive_paths = (
                _archive_patch_paths(preview.get("archive_diff"))
                if archive_policy == "post_merge" and isinstance(preview, dict)
                else set()
            )

        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[github_repository_id],
            permissions={"contents": "write", "pull_requests": "write"},
        )

        async def request(
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            params: dict[str, str | int] | None = None,
        ) -> dict[str, Any] | list[Any]:
            return await client._request(method, path, token=token, body=body, params=params)

        repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        remote_repo = await request("GET", repo_path)
        if not isinstance(remote_repo, dict) or not isinstance(
            remote_repo.get("default_branch"), str
        ):
            raise GitHubAppError("GitHub repository response omitted its default branch")
        base_branch = remote_repo["default_branch"]
        branch = f"harnessa/task-{task_id.hex[:12]}-{bundle_digest[:12]}"
        existing_prs = await request(
            "GET",
            f"{repo_path}/pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "base": base_branch},
        )
        if isinstance(existing_prs, list):
            matching_pr = next(
                (
                    item
                    for item in existing_prs
                    if isinstance(item, dict)
                    and isinstance(item.get("head"), dict)
                    and item["head"].get("ref") == branch
                ),
                None,
            )
            if matching_pr is not None:
                pr_body = matching_pr.get("body")
                if (
                    not isinstance(pr_body, str)
                    or f"{_MARKER_PREFIX} {bundle_digest}" not in pr_body
                ):
                    return EffectResult(
                        "applied", {"state": "pull_request_conflict", "branch": branch}
                    )
                result = _pr_result(matching_pr, bundle_digest, base_sha, head_sha)
                pr_head = matching_pr.get("head")
                branch_ref_value = await request(
                    "GET", f"{repo_path}/git/ref/heads/{quote(branch, safe='')}"
                )
                branch_sha = _ref_sha(branch_ref_value)
                if not isinstance(pr_head, dict) or pr_head.get("sha") != branch_sha:
                    result["state"] = "pull_request_drift"
                result["branch_sha"] = branch_sha
                await self._persist_pr_sync(
                    organization_id,
                    project_id,
                    repository_link_id,
                    task_id,
                    bundle_id,
                    bundle_digest,
                    result,
                    branch,
                )
                return EffectResult("applied", result)
        ref_path = f"{repo_path}/git/ref/heads/{quote(base_branch, safe='')}"
        remote_ref = await request("GET", ref_path)
        remote_base_sha = _ref_sha(remote_ref)
        if remote_base_sha != base_sha:
            return EffectResult(
                "applied",
                {
                    "state": "stale_base",
                    "expected_base_sha": base_sha,
                    "remote_base_sha": remote_base_sha,
                },
            )

        branch_ref_path = f"{repo_path}/git/ref/heads/{quote(branch, safe='')}"
        branch_ref: dict[str, Any] | None
        try:
            branch_ref = _as_dict(await request("GET", branch_ref_path))
        except GitHubAppError as exc:
            if exc.status_code != 404:
                raise
            branch_ref = None
        if branch_ref is None:
            tree_entries = await asyncio.to_thread(
                _review_tree_entries, source_path, base_sha, head_sha, archive_paths
            )
            base_commit = await request("GET", f"{repo_path}/git/commits/{base_sha}")
            if not isinstance(base_commit, dict) or not isinstance(base_commit.get("tree"), dict):
                raise GitHubAppError("GitHub base commit did not include its tree")
            tree_entries = await _upload_blobs(
                request, repo_path, source_path, head_sha, tree_entries
            )
            tree = await request(
                "POST",
                f"{repo_path}/git/trees",
                {"base_tree": base_commit["tree"].get("sha"), "tree": tree_entries},
            )
            if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
                raise GitHubAppError("GitHub did not return the review tree")
            commit = await request(
                "POST",
                f"{repo_path}/git/commits",
                {
                    "message": f"Publish Harnessa review bundle {bundle_digest}\n\n{_MARKER_PREFIX} {bundle_digest}",
                    "tree": tree["sha"],
                    "parents": [base_sha],
                },
            )
            if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
                raise GitHubAppError("GitHub did not return the review commit")
            try:
                await request(
                    "POST",
                    f"{repo_path}/git/refs",
                    {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
                )
            except GitHubAppError as exc:
                if exc.status_code != 422:
                    raise
                branch_ref = _as_dict(await request("GET", branch_ref_path))
            if branch_ref is None:
                branch_ref = {"object": {"sha": commit["sha"]}}
        branch_sha = _ref_sha(branch_ref)
        branch_commit = (
            await request("GET", f"{repo_path}/git/commits/{branch_sha}") if branch_sha else {}
        )
        commit_message = branch_commit.get("message", "") if isinstance(branch_commit, dict) else ""
        if (
            not isinstance(commit_message, str)
            or f"{_MARKER_PREFIX} {bundle_digest}" not in commit_message
        ):
            return EffectResult(
                "applied", {"state": "branch_conflict", "branch": branch, "branch_sha": branch_sha}
            )

        marker = f"<!-- {_MARKER_PREFIX} {bundle_digest} -->"
        pr = await request(
            "POST",
            f"{repo_path}/pulls",
            {
                "title": f"Harnessa review: {bundle_digest[:12]}",
                "head": branch,
                "base": base_branch,
                "body": f"Automated review bundle for task `{task_id}`.\n\nBundle: `{bundle_digest}`\nVerified source head: `{head_sha}`\nBase: `{base_sha}`\n\n{marker}",
                "maintainer_can_modify": False,
            },
        )
        if not isinstance(pr, dict):
            raise GitHubAppError("GitHub did not return the created pull request")
        result = _pr_result(pr, bundle_digest, base_sha, head_sha)
        result["branch_sha"] = branch_sha
        if result.get("head_sha") != branch_sha or result.get("base_sha") != remote_base_sha:
            result["state"] = "pull_request_drift"
        await self._persist_pr_sync(
            organization_id,
            project_id,
            repository_link_id,
            task_id,
            bundle_id,
            bundle_digest,
            result,
            branch,
        )
        return EffectResult("applied", result)

    async def _reconcile_issue_create(
        self, stable_key: str, payload: Mapping[str, object]
    ) -> EffectResult | ReconcileOutcome:
        try:
            link_id = UUID(str(payload["repository_link_id"]))
            task_id = UUID(str(payload["task_id"]))
            title = str(payload["title"])
            body = str(payload["body"])
        except (KeyError, ValueError):
            raise GitHubAppError("GitHub issue creation intent is invalid") from None
        if not title.strip() or len(title) > 256 or len(body) > 60_000 or len(stable_key) > 180:
            raise GitHubAppError("GitHub issue creation intent exceeds its limits")
        async with self.session_factory() as db:
            link = await db.get(GitHubRepositoryLink, link_id)
            if link is None or not link.opted_in or link.setup_status != "ready":
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "repository_not_opted_in"}
                )
            installation = await db.get(GitHubInstallation, link.installation_id)
            app_config = (
                await db.get(GitHubAppConfiguration, installation.app_configuration_id)
                if installation
                else None
            )
            if (
                installation is None
                or app_config is None
                or not installation.active
                or not app_config.enabled
            ):
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "installation_unavailable"}
                )
            app_id = app_config.app_id
            private_key_ref = app_config.private_key_secret_ref
            installation_number = installation.installation_id
            repository_id = link.github_repository_id
            owner, name, topic_label = link.owner, link.name, link.topic_label
            organization_id, project_id = link.organization_id, link.project_id
        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[repository_id],
            permissions={"issues": "write"},
        )
        repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        marker = f"<!-- Harnessa-Issue: {stable_key} -->"
        existing: dict[str, Any] | None = None
        for page in range(1, 101):
            rows = await client._request(
                "GET",
                f"{repo_path}/issues",
                token=token,
                params={"state": "all", "per_page": 100, "page": page},
            )
            if not isinstance(rows, list):
                raise GitHubAppError("GitHub issues response is invalid")
            existing = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and "pull_request" not in row
                    and isinstance(row.get("body"), str)
                    and marker in row["body"]
                ),
                None,
            )
            if existing is not None or len(rows) < 100:
                break
        else:
            return EffectResult(
                "unknown", {"state": "issue_search_limit", "stable_key": stable_key}
            )
        issue = existing
        if issue is None:
            created = await client._request(
                "POST",
                f"{repo_path}/issues",
                token=token,
                body={
                    "title": title,
                    "body": f"{body.rstrip()}\n\n{marker}",
                    "labels": [topic_label],
                },
            )
            issue = created if isinstance(created, dict) else None
        if (
            issue is None
            or not isinstance(issue.get("id"), int)
            or not isinstance(issue.get("number"), int)
        ):
            raise GitHubAppError("GitHub did not return a stable issue identity")
        result = {
            "state": str(issue.get("state") or "open"),
            "issue_id": issue["id"],
            "issue_number": issue["number"],
            "issue_url": issue.get("html_url"),
            "stable_key": stable_key,
        }
        async with self.session_factory() as db:
            row = await db.scalar(
                select(GitHubSyncObject)
                .where(
                    GitHubSyncObject.repository_link_id == link_id,
                    GitHubSyncObject.object_kind == "issue",
                    GitHubSyncObject.github_object_id == str(issue["id"]),
                )
                .with_for_update()
            )
            if row is None:
                row = GitHubSyncObject(
                    organization_id=organization_id,
                    project_id=project_id,
                    repository_link_id=link_id,
                    task_id=task_id,
                    object_kind="issue",
                    github_object_id=str(issue["id"]),
                    object_number=issue["number"],
                    stable_key=f"github-issue:{link_id}:{issue['id']}",
                    state=result["state"],
                    url=result["issue_url"] if isinstance(result["issue_url"], str) else None,
                    check_results={},
                    field_baseline={"title": title, "issue_effect_key": stable_key},
                    sync_conflicts={},
                )
                db.add(row)
            await db.commit()
        return EffectResult("applied", result)

    async def _reconcile_commit_status(
        self, stable_key: str, payload: Mapping[str, object]
    ) -> EffectResult | ReconcileOutcome:
        try:
            link_id = UUID(str(payload["repository_link_id"]))
            task_id = UUID(str(payload["task_id"]))
            sha = str(payload["sha"])
            state = str(payload["state"])
            description = str(payload["description"])
            context = str(payload["context"])
            target_url = str(payload["target_url"]) if payload.get("target_url") else None
        except (KeyError, ValueError):
            raise GitHubAppError("GitHub commit status intent is invalid") from None
        if (
            not re.fullmatch(r"[0-9a-f]{40,64}", sha)
            or state not in {"pending", "success", "error", "failure"}
            or not context.startswith("harnessa/task/")
            or len(context) > 100
            or len(description) > 140
            or (
                target_url is not None
                and (
                    urlsplit(target_url).scheme != "https"
                    or not urlsplit(target_url).hostname
                    or urlsplit(target_url).username is not None
                    or urlsplit(target_url).password is not None
                )
            )
        ):
            raise GitHubAppError("GitHub commit status intent contains invalid fields")
        async with self.session_factory() as db:
            link = await db.get(GitHubRepositoryLink, link_id)
            task = await db.get(Task, task_id)
            expected_version = payload.get("task_version")
            if task is None or not isinstance(expected_version, int):
                return EffectResult("applied", {"state": "rejected", "reason": "task_unavailable"})
            if task.version != expected_version:
                return EffectResult("applied", {"state": "stale", "reason": "task_version_changed"})
            if link is None or not link.opted_in or link.setup_status != "ready":
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "repository_not_opted_in"}
                )
            installation = await db.get(GitHubInstallation, link.installation_id)
            app_config = (
                await db.get(GitHubAppConfiguration, installation.app_configuration_id)
                if installation
                else None
            )
            if (
                installation is None
                or app_config is None
                or not installation.active
                or not app_config.enabled
            ):
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "installation_unavailable"}
                )
            app_id, private_key_ref = app_config.app_id, app_config.private_key_secret_ref
            installation_number, repository_id = (
                installation.installation_id,
                link.github_repository_id,
            )
            owner, name = link.owner, link.name
        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[repository_id],
            permissions={"statuses": "write"},
        )
        repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        statuses = await client._request(
            "GET",
            f"{repo_path}/commits/{sha}/statuses",
            token=token,
            params={"per_page": 100},
        )
        if not isinstance(statuses, list):
            raise GitHubAppError("GitHub commit statuses response is invalid")
        latest = next(
            (row for row in statuses if isinstance(row, dict) and row.get("context") == context),
            None,
        )
        status: dict[str, Any] | None
        if latest is not None and all(
            latest.get(key) == value
            for key, value in (
                ("state", state),
                ("description", description),
                ("target_url", target_url),
            )
        ):
            status = latest
        else:
            created = await client._request(
                "POST",
                f"{repo_path}/statuses/{sha}",
                token=token,
                body={
                    "state": state,
                    "description": description,
                    "context": context,
                    "target_url": target_url,
                },
            )
            status = created if isinstance(created, dict) else None
        if status is None or not isinstance(status.get("id"), int):
            raise GitHubAppError("GitHub did not return a commit status identity")
        return EffectResult(
            "applied",
            {
                "state": state,
                "status_id": status["id"],
                "context": context,
                "stable_key": stable_key,
            },
        )

    async def _reconcile_issue_comment(
        self, stable_key: str, payload: Mapping[str, object]
    ) -> EffectResult | ReconcileOutcome:
        try:
            link_id = UUID(str(payload["repository_link_id"]))
            issue_number_value = payload["issue_number"]
            if not isinstance(issue_number_value, int) or isinstance(issue_number_value, bool):
                raise TypeError
            issue_number = issue_number_value
            comment = str(payload["comment"])
            marker_value = str(payload["marker"])
        except (KeyError, TypeError, ValueError):
            raise GitHubAppError("GitHub issue comment intent is invalid") from None
        if issue_number <= 0 or len(comment) > 8000 or len(marker_value) > 200:
            raise GitHubAppError("GitHub issue comment intent exceeds its limit")
        async with self.session_factory() as db:
            link = await db.get(GitHubRepositoryLink, link_id)
            if link is None or not link.opted_in or link.setup_status != "ready":
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "repository_not_opted_in"}
                )
            installation = await db.get(GitHubInstallation, link.installation_id)
            app_config = (
                await db.get(GitHubAppConfiguration, installation.app_configuration_id)
                if installation
                else None
            )
            if (
                installation is None
                or app_config is None
                or not installation.active
                or not app_config.enabled
            ):
                return EffectResult(
                    "applied", {"state": "rejected", "reason": "installation_unavailable"}
                )
            app_id = app_config.app_id
            private_key_ref = app_config.private_key_secret_ref
            installation_number = installation.installation_id
            repository_id = link.github_repository_id
            owner, name = link.owner, link.name
        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[repository_id],
            permissions={"issues": "write"},
        )
        repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        comments_path = f"{repo_path}/issues/{issue_number}/comments"
        marker = f"<!-- Harnessa-Effect: {marker_value} -->"
        for page in range(1, 101):
            rows = await client._request(
                "GET",
                comments_path,
                token=token,
                params={"per_page": 100, "page": page},
            )
            if not isinstance(rows, list):
                raise GitHubAppError("GitHub issue comments response is invalid")
            for row in rows:
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("body"), str)
                    and marker in row["body"]
                ):
                    return EffectResult(
                        "applied",
                        {
                            "state": "comment",
                            "comment_id": row.get("id"),
                            "comment_url": row.get("html_url"),
                            "stable_key": stable_key,
                        },
                    )
            if len(rows) < 100:
                break
        else:
            return EffectResult(
                "unknown", {"state": "comment_search_limit", "stable_key": stable_key}
            )
        created = await client._request(
            "POST",
            comments_path,
            token=token,
            body={"body": f"{comment}\n\n{marker}"},
        )
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise GitHubAppError("GitHub did not return the created issue comment")
        return EffectResult(
            "applied",
            {
                "state": "comment",
                "comment_id": created["id"],
                "comment_url": created.get("html_url"),
                "stable_key": stable_key,
            },
        )

    async def _persist_pr_sync(
        self,
        organization_id: UUID,
        project_id: UUID,
        repository_link_id: UUID,
        task_id: UUID,
        bundle_id: UUID,
        bundle_digest: str,
        result: dict[str, Any],
        branch: str,
    ) -> None:
        remote_id = result.get("pull_request_id")
        number = result.get("pull_request_number")
        if not isinstance(remote_id, int) or not isinstance(number, int):
            raise GitHubAppError("GitHub pull request identity is incomplete")
        async with self.session_factory() as db:
            row = await db.scalar(
                select(GitHubSyncObject)
                .where(
                    GitHubSyncObject.repository_link_id == repository_link_id,
                    GitHubSyncObject.object_kind == "pull_request",
                    GitHubSyncObject.github_object_id == str(remote_id),
                )
                .with_for_update()
            )
            if row is None:
                row = GitHubSyncObject(
                    organization_id=organization_id,
                    project_id=project_id,
                    repository_link_id=repository_link_id,
                    task_id=task_id,
                    object_kind="pull_request",
                    github_object_id=str(remote_id),
                    object_number=number,
                    stable_key=f"github-pr:{repository_link_id}:{remote_id}",
                    state=str(result.get("state_remote") or "unknown"),
                    check_results={},
                    field_baseline={},
                    sync_conflicts={},
                )
                db.add(row)
            row.task_id = task_id
            row.object_number = number
            row.url = (
                result.get("pull_request_url")
                if isinstance(result.get("pull_request_url"), str)
                else None
            )
            row.state = str(result.get("state_remote") or result.get("state") or "unknown")
            row.base_sha = (
                result.get("base_sha") if isinstance(result.get("base_sha"), str) else None
            )
            row.head_sha = (
                result.get("head_sha") if isinstance(result.get("head_sha"), str) else None
            )
            row.field_baseline = {
                "bundle_id": str(bundle_id),
                "bundle_digest": bundle_digest,
                "expected_source_head_sha": result.get("verified_source_head_sha"),
                "published_base_sha": result.get("expected_base_sha"),
                "published_branch": branch,
                "published_branch_sha": result.get("branch_sha"),
            }
            row.sync_conflicts = (
                {"publication_state": result.get("state")}
                if result.get("state") != "pull_request"
                else {}
            )
            await db.commit()


def _as_dict(value: dict[str, Any] | list[Any]) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _ref_sha(value: dict[str, Any] | list[Any] | None) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("object"), dict):
        sha = value["object"].get("sha")
        return sha if isinstance(sha, str) else None
    return None


def _review_tree_entries(
    source: Path, base_sha: str, head_sha: str, excluded_paths: set[str] | None = None
) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", base_sha, head_sha],
        cwd=source,
        check=True,
        capture_output=True,
        timeout=30,
    )
    parts = result.stdout.decode("utf-8", errors="strict").split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index]
        index += 1
        if status not in {"A", "M", "D"} or index >= len(parts):
            raise GitHubAppError("Review bundle contains an unsupported Git change type")
        relative = PurePosixPath(parts[index])
        index += 1
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise GitHubAppError("Review bundle contains an invalid repository path")
        if relative.as_posix() in (excluded_paths or set()):
            continue
        entry: dict[str, Any] = {"path": relative.as_posix(), "mode": "100644", "type": "blob"}
        if status == "D":
            entry["sha"] = None
        else:
            entry["local_path"] = relative.as_posix()
            entry["mode"] = _git_mode(source, head_sha, relative.as_posix())
        entries.append(entry)
    if not entries:
        raise GitHubAppError("Review bundle has no file changes to publish")
    return entries


def _archive_patch_paths(patch: object) -> set[str]:
    return set(_archive_patch_changes(patch))


def _archive_patch_changes(patch: object) -> dict[str, bool]:
    if not isinstance(patch, str) or not patch:
        raise GitHubAppError("Post-merge archive policy requires a prepared archive diff")
    changes: dict[str, bool] = {}
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for section in sections:
        first_line = section.splitlines()[0] if section else ""
        if not first_line.startswith("diff --git a/"):
            continue
        match = re.fullmatch(r"diff --git a/([^\t]+) b/([^\t]+)", first_line)
        if match is None or match.group(1) != match.group(2):
            raise GitHubAppError("Archive diff contains an unsupported repository path")
        path = PurePosixPath(match.group(1))
        if (
            path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or len(path.parts) < 3
            or path.parts[0] != "openspec"
            or path.parts[1] not in {"specs", "changes"}
        ):
            raise GitHubAppError("Archive diff contains an invalid repository path")
        changes[path.as_posix()] = "deleted file mode " in section or "+++ /dev/null" in section
    if not changes:
        raise GitHubAppError("Post-merge archive diff contains no file changes")
    return changes


def _git_mode(source: Path, revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "ls-tree", "-z", revision, "--", path],
        cwd=source,
        check=True,
        capture_output=True,
        timeout=30,
    )
    record = result.stdout.decode("utf-8", errors="strict").split("\0", 1)[0]
    mode = record.split(" ", 1)[0] if record else ""
    if mode not in {"100644", "100755", "120000"}:
        raise GitHubAppError("Review bundle contains an unsupported file mode")
    return mode


async def _upload_blobs(
    request: Any, repo_path: str, source: Path, head_sha: str, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    for entry in entries:
        item = {key: value for key, value in entry.items() if key != "local_path"}
        local_path = entry.get("local_path")
        if isinstance(local_path, str):
            pathspec = f"{head_sha}:{local_path}"
            content = await asyncio.to_thread(
                subprocess.run,
                ["git", "cat-file", "blob", pathspec],
                cwd=source,
                check=True,
                capture_output=True,
                timeout=30,
            )
            blob = await request(
                "POST",
                f"{repo_path}/git/blobs",
                {
                    "content": base64.b64encode(content.stdout).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if not isinstance(blob, dict) or not isinstance(blob.get("sha"), str):
                raise GitHubAppError("GitHub did not return a file blob")
            item["sha"] = blob["sha"]
        uploaded.append(item)
    return uploaded


def _pr_result(
    pr: dict[str, Any], bundle_digest: str, base_sha: str, head_sha: str
) -> dict[str, Any]:
    head_value = pr.get("head")
    base_value = pr.get("base")
    head: dict[str, Any] = head_value if isinstance(head_value, dict) else {}
    base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
    return {
        "state": "pull_request",
        "bundle_digest": bundle_digest,
        "pull_request_id": pr.get("id"),
        "pull_request_number": pr.get("number"),
        "pull_request_url": pr.get("html_url"),
        "state_remote": pr.get("state"),
        "merged": pr.get("merged") is True,
        "head_sha": head.get("sha"),
        "base_sha": base.get("sha"),
        "verified_source_head_sha": head_sha,
        "expected_base_sha": base_sha,
    }
