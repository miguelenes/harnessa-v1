from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from harnessa.adapters import github_effects
from harnessa.adapters.github_app import (
    APP_REQUIRED_PERMISSIONS,
    GitHubAppClient,
    GitHubAppError,
    check_permissions,
    resolve_secret_reference,
    verify_webhook_signature,
)
from harnessa.adapters.github_effects import (
    GitHubEffectReconciler,
    _archive_patch_changes,
    _review_tree_entries,
)
from harnessa.adapters.specification_workspace import SpecificationWorkspaceError
from harnessa.db.github import (
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
)
from harnessa.db.models import Repository, Task
from harnessa.db.review import IntegrationEvidence, ReviewBundle
from harnessa.services.github_processor import (
    GitHubWebhookProcessor,
    _authorized_latest_review_states,
    _latest_review_state_by_actor,
    _pull_request_state,
)
from harnessa.services.post_integration import _archive_result


def test_secret_reference_and_raw_body_signature_are_narrow_and_exact() -> None:
    secret = "fixture-only-secret"
    body = b'{"action":"opened"}'
    import hashlib
    import hmac

    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, signature, secret)
    assert not verify_webhook_signature(body + b" ", signature, secret)
    assert not verify_webhook_signature(body, "sha1=" + signature[7:], secret)
    assert (
        resolve_secret_reference(
            "test-private-key",
            references_json='{"test-private-key":"TEST_GITHUB_PRIVATE_KEY"}',
            environ={"TEST_GITHUB_PRIVATE_KEY": "fixture-value"},
        )
        == "fixture-value"
    )
    with pytest.raises(GitHubAppError, match="not configured"):
        resolve_secret_reference("DATABASE_URL", references_json="{}", environ={})


def test_permission_check_rejects_missing_and_lower_grants() -> None:
    granted = {**APP_REQUIRED_PERMISSIONS, "contents": "read"}
    assert check_permissions(granted, APP_REQUIRED_PERMISSIONS) == ["contents:write"]
    assert check_permissions(APP_REQUIRED_PERMISSIONS, APP_REQUIRED_PERMISSIONS) == []


@pytest.mark.anyio
async def test_closed_or_drifted_pull_request_queues_bounded_recovery() -> None:
    task = Task(
        id=uuid4(),
        project_id=uuid4(),
        title="Tracked task",
        stage="HUMAN_REVIEW",
        run_status="SUCCEEDED",
        version=7,
    )
    sync = GitHubSyncObject(
        id=uuid4(),
        task_id=task.id,
        head_sha="a" * 40,
        field_baseline={},
        sync_conflicts={},
    )

    class FakeDB:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.added: list[object] = []

        async def scalar(self, _statement: object) -> object:
            self.scalar_calls += 1
            return task if self.scalar_calls == 1 else 1

        def add(self, item: object) -> None:
            self.added.append(item)

    db = FakeDB()
    await GitHubWebhookProcessor()._recover_unmerged_review(
        db,  # type: ignore[arg-type]
        sync=sync,
        reason="GitHub pull request was closed without merging",
        reason_code="closed_unmerged",
    )

    assert task.stage == "READY"
    assert task.run_status == "QUEUED"
    assert task.version == 8
    assert sync.sync_conflicts["recovery_marker"].endswith(":closed_unmerged")
    assert len(db.added) == 1


def test_latest_review_state_overrides_a_historical_approval() -> None:
    reviewer_id = uuid4()
    latest = _latest_review_state_by_actor(
        [
            {
                "id": 10,
                "state": "APPROVED",
                "commit_id": "a" * 40,
                "submitted_at": "2026-01-01T00:00:00Z",
                "user": {"id": 123},
            },
            {
                "id": 11,
                "state": "CHANGES_REQUESTED",
                "commit_id": "a" * 40,
                "submitted_at": "2026-01-02T00:00:00Z",
                "user": {"id": 123},
            },
            {
                "id": 12,
                "state": "APPROVED",
                "commit_id": "b" * 40,
                "submitted_at": "2026-01-03T00:00:00Z",
                "user": {"id": 123},
            },
        ],
        {123: reviewer_id},
        "a" * 40,
    )

    assert latest[reviewer_id]["state"] == "CHANGES_REQUESTED"


def test_unauthorized_changes_request_cannot_veto_merge_verification() -> None:
    authorized_reviewer = uuid4()
    unauthorized_reviewer = uuid4()
    latest = {
        authorized_reviewer: {"state": "APPROVED"},
        unauthorized_reviewer: {"state": "CHANGES_REQUESTED"},
    }

    filtered = _authorized_latest_review_states(latest, {authorized_reviewer})

    assert filtered == {authorized_reviewer: {"state": "APPROVED"}}


def test_pull_request_reconciliation_classifies_base_and_merged_head_drift() -> None:
    assert (
        _pull_request_state(
            merged=False,
            remote_state="open",
            head_matches=True,
            base_matches=False,
        )
        == "base_drift"
    )
    assert (
        _pull_request_state(
            merged=True,
            remote_state="closed",
            head_matches=False,
            base_matches=False,
        )
        == "merged_head_drift"
    )


def test_archive_finalization_binds_the_verified_bundle_and_merge_revision() -> None:
    head = "a" * 40
    published_head = "c" * 40
    merge = "b" * 40
    archive_diff = "diff --git a/openspec/specs/x/spec.md b/openspec/specs/x/spec.md\n"
    digest = hashlib.sha256(archive_diff.encode()).hexdigest()
    integration = SimpleNamespace(
        mode="github",
        evidence={
            "verified_source_head_sha": head,
            "github_head_sha": published_head,
            "published_branch_sha": published_head,
            "merge_commit_sha": merge,
        },
        integration_revision=merge,
    )
    bundle = SimpleNamespace(
        head_sha=head,
        bundle={
            "openspec_sync_preview": {
                "archive_preview_ready": True,
                "final_head_verified": True,
                "proposed_final_review_head": head,
                "archive_diff": archive_diff,
                "diff_sha256": digest,
            }
        },
    )

    result = _archive_result(integration, bundle)

    assert result == {
        "outcome": "succeeded",
        "policy": "pre_merge_archive",
        "archive_diff_sha256": digest,
        "review_head_sha": head,
        "integration_revision": merge,
        "github_head_sha": published_head,
    }
    bundle.bundle["openspec_sync_preview"]["diff_sha256"] = "0" * 64
    with pytest.raises(SpecificationWorkspaceError, match="stale or invalid"):
        _archive_result(integration, bundle)


def test_archive_finalization_rejects_incomplete_provenance_and_accepts_manual_evidence() -> None:
    archive_diff = "diff --git a/openspec/specs/access/spec.md b/openspec/specs/access/spec.md\n"
    digest = hashlib.sha256(archive_diff.encode()).hexdigest()
    preview = {
        "archive_preview_ready": True,
        "final_head_verified": True,
        "proposed_final_review_head": "a" * 40,
        "archive_diff": archive_diff,
        "diff_sha256": digest,
    }
    bundle = SimpleNamespace(head_sha="a" * 40, bundle={"openspec_sync_preview": preview})
    github_integration = SimpleNamespace(
        mode="github",
        evidence={
            "verified_source_head_sha": "a" * 40,
            "github_head_sha": "b" * 40,
            "published_branch_sha": "b" * 40,
            "merge_commit_sha": "c" * 40,
        },
        integration_revision="c" * 40,
    )
    for changed in (
        {"archive_preview_ready": False},
        {"final_head_verified": False},
        {"proposed_final_review_head": "d" * 40},
        {"diff_sha256": "invalid"},
        {"archive_diff": "changed"},
    ):
        invalid_preview = {**preview, **changed}
        with pytest.raises(SpecificationWorkspaceError, match="stale or invalid"):
            _archive_result(
                github_integration,
                SimpleNamespace(
                    head_sha=bundle.head_sha, bundle={"openspec_sync_preview": invalid_preview}
                ),
            )
    with pytest.raises(SpecificationWorkspaceError, match="merge evidence is incomplete"):
        _archive_result(
            SimpleNamespace(
                mode="github",
                evidence={"verified_source_head_sha": "a" * 40, "merge_commit_sha": "c" * 40},
                integration_revision="c" * 40,
            ),
            bundle,
        )
    with pytest.raises(SpecificationWorkspaceError, match="merge evidence is incomplete"):
        _archive_result(
            SimpleNamespace(
                mode="github",
                evidence={
                    "verified_source_head_sha": "a" * 40,
                    "github_head_sha": "b" * 40,
                    "published_branch_sha": "d" * 40,
                    "merge_commit_sha": "c" * 40,
                },
                integration_revision="c" * 40,
            ),
            bundle,
        )
    manual = SimpleNamespace(
        mode="manual",
        evidence={"operator_confirmation": True},
        integration_revision="c" * 40,
    )
    result = _archive_result(manual, bundle)
    assert result["policy"] == "pre_merge_archive"
    assert "github_head_sha" not in result
    manual.evidence["operator_confirmation"] = False
    with pytest.raises(SpecificationWorkspaceError, match="operator confirmation"):
        _archive_result(manual, bundle)


def test_post_merge_archive_diff_is_limited_to_safe_openspec_paths() -> None:
    patch = (
        "diff --git a/openspec/specs/access/spec.md b/openspec/specs/access/spec.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/openspec/specs/access/spec.md\n"
        "+++ b/openspec/specs/access/spec.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/openspec/changes/sample/tasks.md b/openspec/changes/sample/tasks.md\n"
        "deleted file mode 100644\n"
        "--- a/openspec/changes/sample/tasks.md\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-checked\n"
    )

    assert _archive_patch_changes(patch) == {
        "openspec/specs/access/spec.md": False,
        "openspec/changes/sample/tasks.md": True,
    }
    with pytest.raises(GitHubAppError, match="invalid repository path"):
        _archive_patch_changes(
            "diff --git a/../outside b/../outside\n--- a/../outside\n+++ b/../outside\n"
        )
    for invalid_patch, message in (
        (None, "requires a prepared archive diff"),
        ("diff --git a/README.md b/README.md\n", "invalid repository path"),
        (
            "diff --git a/openspec/unsupported/file.md b/openspec/unsupported/file.md\n",
            "invalid repository path",
        ),
        ("diff --git a/openspec/specs/a.md b/openspec/specs/b.md\n", "unsupported repository path"),
    ):
        with pytest.raises(GitHubAppError, match=message):
            _archive_patch_changes(invalid_patch)


@pytest.mark.anyio
async def test_installation_token_is_explicitly_repository_scoped_and_validated() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(await request.aread())
        assert payload["repository_ids"] == [4321]
        assert payload["permissions"] == APP_REQUIRED_PERMISSIONS
        assert request.url.path == "/app/installations/123/access_tokens"
        return httpx.Response(
            201,
            json={
                "token": "fixture-installation-token",
                "permissions": APP_REQUIRED_PERMISSIONS,
                "repositories": [{"id": 4321}],
            },
        )

    client = GitHubAppClient(
        api_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
        references_json='{"app-key":"GITHUB_KEY"}',
        environ={"GITHUB_KEY": private_pem},
    )
    token, response = await client.create_installation_token(
        app_id=999,
        private_key_reference="app-key",
        installation_id=123,
        repository_ids=[4321],
        permissions=APP_REQUIRED_PERMISSIONS,
    )
    claims = jwt.decode(
        client.app_jwt(999, "app-key"),
        key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False},
    )
    assert token == "fixture-installation-token"
    assert response["repositories"] == [{"id": 4321}]
    assert claims["iss"] == "999"
    assert calls[0].headers["X-GitHub-Api-Version"] == "2022-11-28"

    async def wrong_scope(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token": "fixture-installation-token",
                "permissions": APP_REQUIRED_PERMISSIONS,
                "repositories": [{"id": 4322}],
            },
        )

    client.transport = httpx.MockTransport(wrong_scope)
    with pytest.raises(GitHubAppError, match="not scoped"):
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[4321],
            permissions=APP_REQUIRED_PERMISSIONS,
        )


def test_token_permission_request_rejects_unknown_permission() -> None:
    client = GitHubAppClient(
        api_url="https://api.github.test",
        references_json='{"app-key":"GITHUB_KEY"}',
        environ={"GITHUB_KEY": "not-used-unless-signed"},
    )
    with pytest.raises(GitHubAppError, match="permission levels are invalid"):
        import asyncio

        asyncio.run(
            client.create_installation_token(
                app_id=999,
                private_key_reference="app-key",
                installation_id=123,
                repository_ids=[4321],
                permissions={"administration": "write"},
            )
        )


def test_review_branch_conversion_preserves_paths_deletions_and_executable_modes(tmp_path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "--quiet")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    (tmp_path / "old.txt").write_text("remove me\n")
    (tmp_path / "run.sh").write_text("#!/bin/sh\necho fixture\n")
    (tmp_path / "run.sh").chmod(0o755)
    git("add", "old.txt", "run.sh")
    git("commit", "--quiet", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "old.txt").unlink()
    (tmp_path / "new file.txt").write_text("new content\n")
    (tmp_path / "openspec/changes/archive/2026-10-23-example").mkdir(parents=True)
    archive_path = "openspec/changes/archive/2026-10-23-example/change.md"
    (tmp_path / archive_path).write_text("archived change\n")
    (tmp_path / "run.sh").write_text("#!/bin/sh\necho updated\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "candidate")
    head = git("rev-parse", "HEAD")

    entries = _review_tree_entries(tmp_path, base, head)
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["old.txt"]["sha"] is None
    assert by_path["new file.txt"]["mode"] == "100644"
    assert by_path["run.sh"]["mode"] == "100755"
    post_merge_entries = _review_tree_entries(tmp_path, base, head, {archive_path})
    assert archive_path not in {entry["path"] for entry in post_merge_entries}


@pytest.mark.anyio
async def test_comment_effect_finds_existing_marker_after_remote_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    link_id, installation_id = uuid4(), uuid4()
    link = type(
        "Link",
        (),
        {
            "id": link_id,
            "installation_id": installation_id,
            "opted_in": True,
            "setup_status": "ready",
            "github_repository_id": 4321,
            "owner": "fixture",
            "name": "repo",
        },
    )()
    installation = type(
        "Installation",
        (),
        {
            "installation_id": 123,
            "active": True,
            "app_configuration_id": uuid4(),
        },
    )()
    app = type(
        "App",
        (),
        {
            "app_id": 999,
            "private_key_secret_ref": "app-key",
            "enabled": True,
        },
    )()

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object:
            if model is GitHubRepositoryLink:
                return link
            if model is GitHubInstallation:
                return installation
            if model is GitHubAppConfiguration:
                return app
            raise AssertionError(f"unexpected model: {model}")

    class FakeClient:
        instance: FakeClient

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object] | None]] = []
            FakeClient.instance = self

        async def create_installation_token(
            self, **kwargs: object
        ) -> tuple[str, dict[str, object]]:
            assert kwargs["repository_ids"] == [4321]
            assert kwargs["permissions"] == {"issues": "write"}
            return "fixture-token", {}

        async def _request(
            self, method: str, path: str, **kwargs: object
        ) -> dict[str, object] | list[object]:
            body = kwargs.get("body")
            self.calls.append((method, path, body if isinstance(body, dict) else None))
            if method == "GET":
                return [
                    {
                        "id": 88,
                        "body": "ack <!-- Harnessa-Effect: stable-comment-key -->",
                        "html_url": "https://github.test/comments/88",
                    }
                ]
            return (
                {"id": 89, "body": body["body"], "html_url": "https://github.test/comments/89"}
                if isinstance(body, dict)
                else {}
            )

    monkeypatch.setattr(github_effects, "GitHubAppClient", FakeClient)
    reconciler = GitHubEffectReconciler(session_factory=lambda: FakeDB())  # type: ignore[arg-type]
    result = await reconciler.reconcile(
        kind="github.issue.comment",
        stable_key="stable-comment-key",
        payload={
            "repository_link_id": str(link_id),
            "issue_number": 7,
            "comment": "Interview created",
            "marker": "stable-comment-key",
        },
    )
    assert isinstance(result, github_effects.EffectResult)
    assert result.outcome == "applied"
    assert result.result["comment_id"] == 88
    assert [method for method, _, _ in FakeClient.instance.calls] == ["GET"]


@pytest.mark.anyio
async def test_issue_creation_reconciles_ambiguous_success_by_stable_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_id, installation_id, task_id = uuid4(), uuid4(), uuid4()
    link = type(
        "Link",
        (),
        {
            "id": link_id,
            "installation_id": installation_id,
            "organization_id": uuid4(),
            "project_id": uuid4(),
            "opted_in": True,
            "setup_status": "ready",
            "github_repository_id": 4321,
            "owner": "fixture",
            "name": "repo",
            "topic_label": "harnessa",
        },
    )()
    installation = type(
        "Installation",
        (),
        {"installation_id": 123, "active": True, "app_configuration_id": uuid4()},
    )()
    app = type("App", (), {"app_id": 999, "private_key_secret_ref": "app-key", "enabled": True})()

    class FakeDB:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object:
            if model is GitHubRepositoryLink:
                return link
            if model is GitHubInstallation:
                return installation
            if model is GitHubAppConfiguration:
                return app
            raise AssertionError(f"unexpected model: {model}")

        async def scalar(self, _statement: object) -> None:
            return None

        def add(self, row: object) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object] | None]] = []

        async def create_installation_token(
            self, **kwargs: object
        ) -> tuple[str, dict[str, object]]:
            assert kwargs["permissions"] == {"issues": "write"}
            return "fixture-token", {}

        async def _request(
            self, method: str, path: str, **kwargs: object
        ) -> dict[str, object] | list[object]:
            body = kwargs.get("body")
            self.calls.append((method, path, body if isinstance(body, dict) else None))
            if method == "GET":
                return [
                    {
                        "id": 77,
                        "number": 12,
                        "state": "open",
                        "body": "task <!-- Harnessa-Issue: issue-key -->",
                        "html_url": "https://github.test/issues/12",
                    }
                ]
            raise AssertionError("reconciled issue marker should avoid a duplicate POST")

    fake_client = FakeClient()
    monkeypatch.setattr(github_effects, "GitHubAppClient", lambda: fake_client)
    reconciler = GitHubEffectReconciler(session_factory=lambda: FakeDB())  # type: ignore[arg-type]
    result = await reconciler.reconcile(
        kind="github.issue.create",
        stable_key="issue-key",
        payload={
            "repository_link_id": str(link_id),
            "task_id": str(task_id),
            "title": "A task",
            "body": "task",
        },
    )

    assert isinstance(result, github_effects.EffectResult)
    assert result.result["issue_id"] == 77
    assert [method for method, _, _ in fake_client.calls] == ["GET"]


@pytest.mark.anyio
async def test_commit_status_reconciles_matching_remote_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_id, installation_id, task_id = uuid4(), uuid4(), uuid4()
    link = type(
        "Link",
        (),
        {
            "id": link_id,
            "installation_id": installation_id,
            "opted_in": True,
            "setup_status": "ready",
            "github_repository_id": 4321,
            "owner": "fixture",
            "name": "repo",
        },
    )()
    installation = type(
        "Installation",
        (),
        {"installation_id": 123, "active": True, "app_configuration_id": uuid4()},
    )()
    app = type("App", (), {"app_id": 999, "private_key_secret_ref": "app-key", "enabled": True})()
    task = type("Task", (), {"version": 3})()

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object:
            return {
                GitHubRepositoryLink: link,
                GitHubInstallation: installation,
                GitHubAppConfiguration: app,
                Task: task,
            }[model]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def create_installation_token(
            self, **kwargs: object
        ) -> tuple[str, dict[str, object]]:
            assert kwargs["permissions"] == {"statuses": "write"}
            return "fixture-token", {}

        async def _request(
            self, method: str, path: str, **kwargs: object
        ) -> dict[str, object] | list[object]:
            self.calls.append(method)
            return [
                {
                    "id": 55,
                    "context": "harnessa/task/test",
                    "state": "success",
                    "description": "passed",
                    "target_url": None,
                }
            ]

    fake_client = FakeClient()
    monkeypatch.setattr(github_effects, "GitHubAppClient", lambda: fake_client)
    reconciler = GitHubEffectReconciler(session_factory=lambda: FakeDB())  # type: ignore[arg-type]
    result = await reconciler.reconcile(
        kind="github.commit_status.publish",
        stable_key="status-key",
        payload={
            "repository_link_id": str(link_id),
            "task_id": str(task_id),
            "task_version": 3,
            "sha": "a" * 40,
            "state": "success",
            "description": "passed",
            "context": "harnessa/task/test",
            "target_url": None,
        },
    )

    assert isinstance(result, github_effects.EffectResult)
    assert result.result["status_id"] == 55
    assert fake_client.calls == ["GET"]


@pytest.mark.anyio
async def test_outbound_effects_reject_invalid_and_unavailable_intents() -> None:
    link_id, task_id = uuid4(), uuid4()

    class EmptyDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> None:
            return None

    reconciler = GitHubEffectReconciler(
        session_factory=lambda: EmptyDB()  # type: ignore[arg-type]
    )
    with pytest.raises(GitHubAppError, match="issue creation intent is invalid"):
        await reconciler.reconcile(kind="github.issue.create", stable_key="x", payload={})
    with pytest.raises(GitHubAppError, match="exceeds its limits"):
        await reconciler.reconcile(
            kind="github.issue.create",
            stable_key="issue-key",
            payload={
                "repository_link_id": str(link_id),
                "task_id": str(task_id),
                "title": " ",
                "body": "body",
            },
        )
    with pytest.raises(GitHubAppError, match="exceeds its limits"):
        await reconciler.reconcile(
            kind="github.issue.create",
            stable_key="k" * 181,
            payload={
                "repository_link_id": str(link_id),
                "task_id": str(task_id),
                "title": "A task",
                "body": "body",
            },
        )
    with pytest.raises(GitHubAppError, match="commit status intent is invalid"):
        await reconciler.reconcile(kind="github.commit_status.publish", stable_key="x", payload={})
    with pytest.raises(GitHubAppError, match="contains invalid fields"):
        await reconciler.reconcile(
            kind="github.commit_status.publish",
            stable_key="status-key",
            payload={
                "repository_link_id": str(link_id),
                "task_id": str(task_id),
                "task_version": 1,
                "sha": "not-a-sha",
                "state": "success",
                "description": "status",
                "context": "harnessa/task/test",
            },
        )
    with pytest.raises(GitHubAppError, match="contains invalid fields"):
        await reconciler.reconcile(
            kind="github.commit_status.publish",
            stable_key="status-key",
            payload={
                "repository_link_id": str(link_id),
                "task_id": str(task_id),
                "task_version": 1,
                "sha": "a" * 40,
                "state": "success",
                "description": "x" * 141,
                "context": "harnessa/task/test",
            },
        )
    with pytest.raises(GitHubAppError, match="contains invalid fields"):
        await reconciler.reconcile(
            kind="github.commit_status.publish",
            stable_key="status-key",
            payload={
                "repository_link_id": str(link_id),
                "task_id": str(task_id),
                "task_version": 1,
                "sha": "a" * 40,
                "state": "success",
                "description": "status",
                "context": "harnessa/task/test",
                "target_url": "http://unsafe.test/",
            },
        )
    with pytest.raises(GitHubAppError, match="issue comment intent is invalid"):
        await reconciler.reconcile(kind="github.issue.comment", stable_key="x", payload={})
    with pytest.raises(GitHubAppError, match="exceeds its limit"):
        await reconciler.reconcile(
            kind="github.issue.comment",
            stable_key="comment-key",
            payload={
                "repository_link_id": str(link_id),
                "issue_number": 12,
                "comment": "x" * 8001,
                "marker": "comment-key",
            },
        )
    with pytest.raises(GitHubAppError, match="exceeds its limit"):
        await reconciler.reconcile(
            kind="github.issue.comment",
            stable_key="comment-key",
            payload={
                "repository_link_id": str(link_id),
                "issue_number": 0,
                "comment": "Acknowledged",
                "marker": "comment-key",
            },
        )

    issue = await reconciler.reconcile(
        kind="github.issue.create",
        stable_key="issue-key",
        payload={
            "repository_link_id": str(link_id),
            "task_id": str(task_id),
            "title": "A task",
            "body": "body",
        },
    )
    comment = await reconciler.reconcile(
        kind="github.issue.comment",
        stable_key="comment-key",
        payload={
            "repository_link_id": str(link_id),
            "issue_number": 12,
            "comment": "Acknowledged",
            "marker": "comment-key",
        },
    )
    status = await reconciler.reconcile(
        kind="github.commit_status.publish",
        stable_key="status-key",
        payload={
            "repository_link_id": str(link_id),
            "task_id": str(task_id),
            "task_version": 1,
            "sha": "a" * 40,
            "state": "success",
            "description": "status",
            "context": "harnessa/task/test",
        },
    )
    unknown = await reconciler.reconcile(
        kind="github.unrecognized.effect",
        stable_key="unknown-key",
        payload={},
    )
    assert isinstance(issue, github_effects.EffectResult)
    assert issue.result == {"state": "rejected", "reason": "repository_not_opted_in"}
    assert isinstance(comment, github_effects.EffectResult)
    assert comment.result == {"state": "rejected", "reason": "repository_not_opted_in"}
    assert isinstance(status, github_effects.EffectResult)
    assert status.result == {"state": "rejected", "reason": "task_unavailable"}
    assert unknown == "unknown"


@pytest.mark.anyio
async def test_github_pollers_enforce_bounded_limits() -> None:
    processor = GitHubWebhookProcessor()
    for operation in (
        processor.reconcile_tracked_pull_requests,
        processor.reconcile_installation_access,
    ):
        for limit in (0, 101):
            with pytest.raises(ValueError, match="limit must be between 1 and 100"):
                await operation(limit=limit)


@pytest.mark.anyio
async def test_issue_and_comment_effect_searches_have_a_hard_page_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_id, installation_id, task_id = uuid4(), uuid4(), uuid4()
    link = SimpleNamespace(
        id=link_id,
        installation_id=installation_id,
        opted_in=True,
        setup_status="ready",
        github_repository_id=4321,
        owner="fixture",
        name="repo",
        topic_label="harnessa",
        organization_id=uuid4(),
        project_id=uuid4(),
    )
    installation = SimpleNamespace(
        installation_id=123,
        active=True,
        app_configuration_id=uuid4(),
    )
    app = SimpleNamespace(app_id=999, private_key_secret_ref="app-key", enabled=True)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object:
            return {
                GitHubRepositoryLink: link,
                GitHubInstallation: installation,
                GitHubAppConfiguration: app,
            }[model]

    class FakeClient:
        def __init__(self) -> None:
            self.pages: dict[str, int] = {}

        async def create_installation_token(self, **_: object) -> tuple[str, dict[str, object]]:
            return "fixture-token", {}

        async def _request(
            self,
            _method: str,
            path: str,
            **kwargs: object,
        ) -> list[dict[str, str]]:
            params = kwargs["params"]
            assert isinstance(params, dict)
            self.pages[path] = self.pages.get(path, 0) + 1
            assert params["page"] == self.pages[path]
            return [{"body": "unrelated issue"}] * 100

    fake_client = FakeClient()
    monkeypatch.setattr(github_effects, "GitHubAppClient", lambda: fake_client)
    reconciler = GitHubEffectReconciler(
        session_factory=lambda: FakeDB()  # type: ignore[arg-type]
    )
    issue = await reconciler.reconcile(
        kind="github.issue.create",
        stable_key="bounded-issue-key",
        payload={
            "repository_link_id": str(link_id),
            "task_id": str(task_id),
            "title": "A task",
            "body": "body",
        },
    )
    comment = await reconciler.reconcile(
        kind="github.issue.comment",
        stable_key="bounded-comment-key",
        payload={
            "repository_link_id": str(link_id),
            "issue_number": 12,
            "comment": "Acknowledged",
            "marker": "bounded-comment-key",
        },
    )

    assert isinstance(issue, github_effects.EffectResult)
    assert issue.outcome == "unknown" and issue.result["state"] == "issue_search_limit"
    assert isinstance(comment, github_effects.EffectResult)
    assert comment.outcome == "unknown" and comment.result["state"] == "comment_search_limit"
    assert fake_client.pages == {
        "/repos/fixture/repo/issues": 100,
        "/repos/fixture/repo/issues/12/comments": 100,
    }


@pytest.mark.anyio
async def test_github_app_client_sanitizes_remote_failures_and_invalid_configuration() -> None:
    with pytest.raises(GitHubAppError, match="HTTPS"):
        GitHubAppClient(api_url="http://api.github.test")
    for raw, message in (
        ("not-json", "is invalid"),
        ("[]", "must be an object"),
        ('{"app-key":"lowercase"}', "not configured"),
        ('{"app-key":"MISSING_KEY"}', "unavailable"),
    ):
        with pytest.raises(GitHubAppError, match=message):
            resolve_secret_reference(
                "app-key",
                references_json=raw,
                environ={},
            )
    bad_key_client = GitHubAppClient(
        api_url="https://api.github.test",
        references_json='{"app-key":"GITHUB_KEY"}',
        environ={"GITHUB_KEY": "not a private key"},
    )
    with pytest.raises(GitHubAppError, match="private key is invalid"):
        bad_key_client.app_jwt(999, "app-key")

    async def denied(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="sensitive GitHub body")

    client = GitHubAppClient(
        api_url="https://api.github.test",
        transport=httpx.MockTransport(denied),
    )
    with pytest.raises(GitHubAppError, match="HTTP 403") as denied_error:
        await client._request("GET", "/private", token="secret-token")
    assert "sensitive" not in str(denied_error.value)
    assert denied_error.value.status_code == 403

    async def scalar(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"null")

    client.transport = httpx.MockTransport(scalar)
    with pytest.raises(GitHubAppError, match="unexpected response"):
        await client._request("GET", "/private", token="secret-token")

    async def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2_000_001)

    client.transport = httpx.MockTransport(oversized)
    with pytest.raises(GitHubAppError, match="exceeded 2 MB"):
        await client._request("GET", "/private", token="secret-token")

    async def empty(_: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client.transport = httpx.MockTransport(empty)
    assert await client._request("GET", "/private", token="secret-token") == {}

    async def invalid_json(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    client.transport = httpx.MockTransport(invalid_json)
    with pytest.raises(GitHubAppError, match=r"transport failed \(JSONDecodeError\)"):
        await client._request("GET", "/private", token="secret-token")

    async def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("request contains sensitive data", request=request)

    client.transport = httpx.MockTransport(connect_error)
    with pytest.raises(
        GitHubAppError, match=r"transport failed \(ConnectError\)"
    ) as transport_error:
        await client._request("GET", "/private", token="secret-token")
    assert "sensitive data" not in str(transport_error.value)

    async def primary_rate_limit(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "37"},
            json={"message": "rate limited"},
        )

    client.transport = httpx.MockTransport(primary_rate_limit)
    with pytest.raises(GitHubAppError, match="HTTP 403") as primary_error:
        await client._request("GET", "/private", token="secret-token")
    assert primary_error.value.rate_limited is True
    assert primary_error.value.retry_after_seconds == 37

    async def secondary_rate_limit(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "23"}, json={"message": "limited"})

    client.transport = httpx.MockTransport(secondary_rate_limit)
    with pytest.raises(GitHubAppError, match="HTTP 429") as secondary_error:
        await client._request("GET", "/private", token="secret-token")
    assert secondary_error.value.rate_limited is True
    assert secondary_error.value.retry_after_seconds == 23


@pytest.mark.anyio
async def test_installation_token_rejects_incomplete_github_responses() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    response_body: object = []

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=response_body)

    client = GitHubAppClient(
        api_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
        references_json='{"app-key":"GITHUB_KEY"}',
        environ={"GITHUB_KEY": private_pem},
    )
    with pytest.raises(GitHubAppError, match="explicit repository IDs"):
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[],
            permissions={"issues": "write"},
        )
    with pytest.raises(GitHubAppError, match="response is invalid"):
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[4321],
            permissions={"issues": "write"},
        )
    with pytest.raises(GitHubAppError, match="omitted required fields"):
        response_body = {"token": "fixture-token"}
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[4321],
            permissions={"issues": "write"},
        )

    with pytest.raises(GitHubAppError, match="lacks requested permissions"):
        response_body = {
            "token": "fixture-token",
            "permissions": {"issues": "read"},
            "repositories": [{"id": 4321}],
        }
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[4321],
            permissions={"issues": "write"},
        )

    async def installation_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client.transport = httpx.MockTransport(installation_handler)
    with pytest.raises(GitHubAppError, match="installation response is invalid"):
        await client.get_installation(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
        )
    client.transport = httpx.MockTransport(handler)
    with pytest.raises(GitHubAppError, match="did not confirm"):
        response_body = {
            "token": "fixture-token",
            "permissions": {"issues": "write"},
            "repositories": None,
        }
        await client.create_installation_token(
            app_id=999,
            private_key_reference="app-key",
            installation_id=123,
            repository_ids=[4321],
            permissions={"issues": "write"},
        )


@pytest.mark.anyio
async def test_archive_maintenance_rejects_unavailable_or_untrusted_sources() -> None:
    integration_id = uuid4()
    bundle_id, repository_id = uuid4(), uuid4()
    installation_id, app_id = uuid4(), uuid4()
    valid_diff = "diff --git a/openspec/specs/access/spec.md b/openspec/specs/access/spec.md\n"
    digest = hashlib.sha256(valid_diff.encode()).hexdigest()
    integration = SimpleNamespace(
        id=integration_id,
        status="confirmed",
        bundle_id=bundle_id,
        repository_id=repository_id,
        integration_revision="a" * 40,
        organization_id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
    )
    bundle = SimpleNamespace(
        id=bundle_id,
        head_sha="b" * 40,
        bundle_digest="c" * 64,
        bundle={
            "openspec_sync_preview": {
                "archive_diff": valid_diff,
                "diff_sha256": digest,
            }
        },
    )
    link = SimpleNamespace(
        id=uuid4(),
        archive_policy="post_merge",
        opted_in=True,
        setup_status="ready",
        installation_id=installation_id,
        github_repository_id=4321,
        owner="fixture",
        name="repo",
    )
    installation = SimpleNamespace(
        installation_id=123,
        active=True,
        app_configuration_id=app_id,
    )
    app = SimpleNamespace(app_id=999, private_key_secret_ref="app-key", enabled=True)
    repository = SimpleNamespace(id=repository_id)

    class FakeDB:
        def __init__(self, scenario: str) -> None:
            self.scenario = scenario

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object | None:
            if model is IntegrationEvidence:
                if self.scenario == "missing_integration":
                    return None
                if self.scenario == "unconfirmed":
                    return SimpleNamespace(**{**integration.__dict__, "status": "pending"})
                return integration
            if model is ReviewBundle:
                if self.scenario == "missing_bundle":
                    return None
                if self.scenario == "missing_preview":
                    return SimpleNamespace(**{**bundle.__dict__, "bundle": {"unrelated": True}})
                if self.scenario == "invalid_digest":
                    return SimpleNamespace(
                        **{
                            **bundle.__dict__,
                            "bundle": {
                                "openspec_sync_preview": {
                                    "archive_diff": valid_diff,
                                    "diff_sha256": "0" * 64,
                                }
                            },
                        }
                    )
                return bundle
            if model is Repository:
                return None if self.scenario == "missing_repository" else repository
            if model is GitHubInstallation:
                return None if self.scenario == "missing_installation" else installation
            if model is GitHubAppConfiguration:
                if self.scenario == "disabled_app":
                    return SimpleNamespace(**{**app.__dict__, "enabled": False})
                return app
            raise AssertionError(f"unexpected model: {model}")

        async def scalar(self, _statement: object) -> object | None:
            if self.scenario == "missing_link":
                return None
            if self.scenario == "wrong_archive_policy":
                return SimpleNamespace(**{**link.__dict__, "archive_policy": "pre_merge"})
            return link

    cases = {
        "missing_integration": "Confirmed integration evidence is unavailable",
        "unconfirmed": "Confirmed integration evidence is unavailable",
        "missing_bundle": "Archive maintenance source is unavailable",
        "missing_repository": "Archive maintenance source is unavailable",
        "missing_link": "Post-merge archive policy is not enabled",
        "wrong_archive_policy": "Post-merge archive policy is not enabled",
        "missing_installation": "installation or preview is unavailable",
        "disabled_app": "installation or preview is unavailable",
        "missing_preview": "installation or preview is unavailable",
        "invalid_digest": "diff failed its digest check",
    }
    for scenario, message in cases.items():
        reconciler = GitHubEffectReconciler(
            session_factory=lambda scenario=scenario: FakeDB(scenario)  # type: ignore[arg-type]
        )
        with pytest.raises(GitHubAppError, match=message):
            await reconciler.publish_archive_maintenance(integration_id)


@pytest.mark.anyio
async def test_review_publication_rejects_stale_or_unavailable_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harnessa.api import review_routes

    link_id, bundle_id, repository_id, installation_id = uuid4(), uuid4(), uuid4(), uuid4()
    app_id = uuid4()
    link = SimpleNamespace(
        id=link_id,
        repository_id=repository_id,
        opted_in=True,
        setup_status="ready",
        installation_id=installation_id,
        github_repository_id=4321,
        owner="fixture",
        name="repo",
        archive_policy="pre_merge",
    )
    bundle = SimpleNamespace(
        id=bundle_id,
        repository_id=repository_id,
        status="prepared",
        head_sha="a" * 40,
        bundle_digest="b" * 64,
        base_sha="c" * 40,
        task_id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        bundle={"openspec_sync_preview": {}},
    )
    repository = SimpleNamespace(id=repository_id)
    installation = SimpleNamespace(
        id=installation_id,
        active=True,
        installation_id=123,
        app_configuration_id=app_id,
    )
    app = SimpleNamespace(app_id=999, private_key_secret_ref="app-key", enabled=True)

    class FakeWorkspace:
        async def source_path(self, _repository: object) -> str:
            return "/tmp/not-used"

        async def contains_revision(self, _repository: object, _revision: str) -> bool:
            return False

    class FakeDB:
        def __init__(self, scenario: str) -> None:
            self.scenario = scenario

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, model: object, _: object) -> object | None:
            if model is ReviewBundle:
                if self.scenario == "missing_bundle":
                    return None
                if self.scenario == "changed_bundle":
                    return SimpleNamespace(**{**bundle.__dict__, "repository_id": uuid4()})
                return bundle
            if model is GitHubRepositoryLink:
                return None if self.scenario == "missing_link" else link
            if model is Repository:
                return repository
            if model is GitHubInstallation:
                return (
                    SimpleNamespace(**{**installation.__dict__, "active": False})
                    if self.scenario == "inactive"
                    else installation
                )
            if model is GitHubAppConfiguration:
                return app
            raise AssertionError(f"unexpected model: {model}")

        async def scalar(self, _statement: object) -> None:
            return None

    async def stale_bundle(db: object, _bundle: object) -> str | None:
        return "stale" if getattr(db, "scenario", "") == "stale" else None

    monkeypatch.setattr(review_routes, "_bundle_stale_reason", stale_bundle)
    cases = {
        "missing_bundle": "bundle_or_repository_not_publishable",
        "missing_link": "bundle_or_repository_not_publishable",
        "changed_bundle": "bundle_or_repository_changed",
        "stale": "review_bundle_became_stale",
        "inactive": "installation_unavailable",
        "head_missing": "review_head_not_available_locally",
    }
    for scenario, reason in cases.items():
        reconciler = GitHubEffectReconciler(
            session_factory=lambda scenario=scenario: FakeDB(scenario),  # type: ignore[arg-type]
            workspace_manager=FakeWorkspace(),  # type: ignore[arg-type]
        )
        result = await reconciler.reconcile(
            kind="github.review_bundle.publish",
            stable_key="publish-key",
            payload={"bundle_id": str(bundle_id), "repository_link_id": str(link_id)},
        )
        assert isinstance(result, github_effects.EffectResult)
        assert result.result["reason"] == reason
    stale_reconciler = GitHubEffectReconciler(
        session_factory=lambda: FakeDB("stale")  # type: ignore[arg-type]
    )
    stale_result = await stale_reconciler.reconcile(
        kind="github.review_bundle.publish",
        stable_key="publish-key",
        payload={"bundle_id": str(bundle_id), "repository_link_id": str(link_id)},
    )
    assert isinstance(stale_result, github_effects.EffectResult)
    assert stale_result.result["reason"] == "review_bundle_became_stale"
