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
from harnessa.adapters.github_effects import GitHubEffectReconciler, _review_tree_entries
from harnessa.adapters.specification_workspace import SpecificationWorkspaceError
from harnessa.db.github import (
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
)
from harnessa.db.models import Task
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
    merge = "b" * 40
    archive_diff = "diff --git a/openspec/specs/x/spec.md b/openspec/specs/x/spec.md\n"
    digest = hashlib.sha256(archive_diff.encode()).hexdigest()
    integration = SimpleNamespace(
        mode="github",
        evidence={"github_head_sha": head, "merge_commit_sha": merge},
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
    }
    bundle.bundle["openspec_sync_preview"]["diff_sha256"] = "0" * 64
    with pytest.raises(SpecificationWorkspaceError, match="stale or invalid"):
        _archive_result(integration, bundle)


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
    (tmp_path / "run.sh").write_text("#!/bin/sh\necho updated\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "candidate")
    head = git("rev-parse", "HEAD")

    entries = _review_tree_entries(tmp_path, base, head)
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["old.txt"]["sha"] is None
    assert by_path["new file.txt"]["mode"] == "100644"
    assert by_path["run.sh"]["mode"] == "100755"


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
        "Installation", (), {"installation_id": 123, "active": True, "app_configuration_id": uuid4()}
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

        async def create_installation_token(self, **kwargs: object) -> tuple[str, dict[str, object]]:
            assert kwargs["permissions"] == {"issues": "write"}
            return "fixture-token", {}

        async def _request(self, method: str, path: str, **kwargs: object) -> dict[str, object] | list[object]:
            body = kwargs.get("body")
            self.calls.append((method, path, body if isinstance(body, dict) else None))
            if method == "GET":
                return [{"id": 77, "number": 12, "state": "open", "body": "task <!-- Harnessa-Issue: issue-key -->", "html_url": "https://github.test/issues/12"}]
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
    installation = type("Installation", (), {"installation_id": 123, "active": True, "app_configuration_id": uuid4()})()
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

        async def create_installation_token(self, **kwargs: object) -> tuple[str, dict[str, object]]:
            assert kwargs["permissions"] == {"statuses": "write"}
            return "fixture-token", {}

        async def _request(self, method: str, path: str, **kwargs: object) -> dict[str, object] | list[object]:
            self.calls.append(method)
            return [{"id": 55, "context": "harnessa/task/test", "state": "success", "description": "passed", "target_url": None}]

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
