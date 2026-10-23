"""Reconciled publication of immutable review bundles to a scoped GitHub repository."""

from __future__ import annotations

import asyncio
import base64
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
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
from harnessa.db.models import Repository
from harnessa.db.review import ReviewBundle
from harnessa.db.session import SessionLocal
from harnessa.services.effects import EffectResult, ReconcileOutcome

_PUBLISH_KIND = "github.review_bundle.publish"
_MARKER_PREFIX = "Harnessa-Bundle:"


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

    async def reconcile(
        self, *, kind: str, stable_key: str, payload: Mapping[str, object]
    ) -> EffectResult | ReconcileOutcome:
        if kind == "github.issue.comment":
            return await self._reconcile_issue_comment(stable_key, payload)
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

        client = GitHubAppClient()
        token, _ = await client.create_installation_token(
            app_id=app_id,
            private_key_reference=private_key_ref,
            installation_id=installation_number,
            repository_ids=[github_repository_id],
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
                _review_tree_entries, source_path, base_sha, head_sha
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


def _review_tree_entries(source: Path, base_sha: str, head_sha: str) -> list[dict[str, Any]]:
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
