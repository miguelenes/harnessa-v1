from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request, Response
from harnessa.adapters import github_effects
from harnessa.adapters.github_app import APP_REQUIRED_PERMISSIONS, GitHubAppClient
from harnessa.api import github_routes, review_routes
from harnessa.config import settings
from harnessa.contracts import (
    GitHubActorMappingCreate,
    GitHubAppConfigurationCreate,
    GitHubCommitStatusRequest,
    GitHubDeliveryRepairRequest,
    GitHubInstallationCreate,
    GitHubPublishRequest,
    GitHubRepositoryOptIn,
)
from harnessa.db.github import (
    GitHubAppConfiguration,
    GitHubInstallation,
    GitHubRepositoryLink,
    GitHubSyncObject,
    GitHubWebhookDelivery,
    GitHubWebhookOutbox,
)
from harnessa.db.model_gateway import ModelBudgetPolicy
from harnessa.db.models import Membership, Project, Repository, Task
from harnessa.db.orchestration import EffectIntent, Run
from harnessa.db.review import (
    PostIntegrationStep,
    ReviewBundle,
    ReviewDecision,
    VerificationExecution,
    VerificationManifestRevision,
)
from harnessa.db.session import SessionLocal
from harnessa.db.specification import SpecificationInterview, SpecificationQuestion
from harnessa.services import github_processor
from harnessa.services.effects import reconcile_effect_intents
from harnessa.services.github_processor import GitHubWebhookProcessor
from harnessa.services.github_webhooks import DeliveryEnvelope, process_github_webhook_outbox
from harnessa.services.model_gateway import budget_policy_digest
from harnessa.services.post_integration import process_post_integration_steps
from harnessa.services.review_decisions import has_current_review_approval
from sqlalchemy import select

from tests.integration.test_specifications import (
    _create_ready_interview,
    _git,
    _specification_fixture,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_post_merge_archive_publishes_one_tracked_pr_from_current_base(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, repository_id, actor, repository, manager = await _specification_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "artifact_root", str(tmp_path / "artifacts"))
    monkeypatch.setattr(review_routes, "_manifest_workspace", manager)

    monkeypatch.setattr(
        settings,
        "github_secret_references_json",
        json.dumps({"webhook-key": "GITHUB_WEBHOOK_SECRET", "app-key": "GITHUB_APP_PRIVATE_KEY"}),
    )
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "fixture-webhook-secret")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture-private-key")

    class SetupClient:
        async def get_installation(self, **_kwargs: object) -> dict[str, object]:
            return {
                "app_id": 999,
                "account": {"id": 456, "login": "fixture", "type": "Organization"},
                "repository_selection": "selected",
                "permissions": APP_REQUIRED_PERMISSIONS,
            }

        async def request_installation(self, **_kwargs: object) -> dict[str, object]:
            return {"id": 4321, "full_name": "fixture/specifications"}

    monkeypatch.setattr(github_routes, "_client", SetupClient)
    with pytest.raises(HTTPException) as bad_secret_reference:
        async with SessionLocal() as db:
            await github_routes.register_github_app(
                GitHubAppConfigurationCreate(
                    app_id=999,
                    webhook_id=124,
                    private_key_secret_ref="missing-key",
                    webhook_secret_ref="webhook-key",
                ),
                actor,
                db,
            )
    assert bad_secret_reference.value.status_code == 422
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as missing_app:
            await github_routes.register_github_installation(
                GitHubInstallationCreate(app_configuration_id=uuid4(), installation_id=456),
                actor,
                db,
            )
        assert missing_app.value.status_code == 404
        app = await github_routes.register_github_app(
            GitHubAppConfigurationCreate(
                app_id=999,
                webhook_id=123,
                private_key_secret_ref="app-key",
                webhook_secret_ref="webhook-key",
            ),
            actor,
            db,
        )
        app_row = await db.get(GitHubAppConfiguration, app.id)
        assert app_row is not None

        class WrongAppClient:
            async def get_installation(self, **_kwargs: object) -> dict[str, object]:
                return {"app_id": 1000}

        with pytest.raises(HTTPException) as wrong_app:
            await github_routes._refresh_installation(
                db, app_config=app_row, installation_id=456, client=WrongAppClient()
            )
        assert wrong_app.value.status_code == 409

        class MissingAccountClient:
            async def get_installation(self, **_kwargs: object) -> dict[str, object]:
                return {"app_id": app.app_id}

        with pytest.raises(HTTPException) as missing_account:
            await github_routes._refresh_installation(
                db, app_config=app_row, installation_id=456, client=MissingAccountClient()
            )
        assert missing_account.value.status_code == 502

        class UnavailableClient:
            async def get_installation(self, **_kwargs: object) -> dict[str, object]:
                raise github_routes.GitHubAppError("fixture unavailable")

        with pytest.raises(HTTPException) as unavailable_installation:
            await github_routes._refresh_installation(
                db, app_config=app_row, installation_id=456, client=UnavailableClient()
            )
        assert unavailable_installation.value.status_code == 502

        class MissingPermissionsClient:
            async def get_installation(self, **_kwargs: object) -> dict[str, object]:
                return {
                    "app_id": app.app_id,
                    "account": {"id": 456, "login": "fixture", "type": "Organization"},
                    "repository_selection": "unexpected",
                    "permissions": {"contents": "read"},
                }

        limited_installation = await github_routes._refresh_installation(
            db, app_config=app_row, installation_id=457, client=MissingPermissionsClient()
        )
        assert limited_installation.active is False
        assert limited_installation.repository_selection == "unknown"
        assert app_row.connection_status == "permission_missing"
        installation = await github_routes.register_github_installation(
            GitHubInstallationCreate(app_configuration_id=app.id, installation_id=456),
            actor,
            db,
        )
        with pytest.raises(HTTPException) as missing_installation:
            await github_routes.refresh_github_installation(uuid4(), actor, db)
        assert missing_installation.value.status_code == 404
        refreshed = await github_routes.refresh_github_installation(installation.id, actor, db)
        assert refreshed.active is True
        opt_in = GitHubRepositoryOptIn(
            installation_id=installation.id,
            github_repository_id=4321,
            required_checks=["Local CI"],
            topic_label="harnessa",
            archive_policy="post_merge",
        )
        link = await github_routes.opt_in_github_repository(
            project_id,
            repository_id,
            opt_in,
            actor,
            db,
        )
        mapping_request = GitHubActorMappingCreate(
            installation_id=installation.id,
            github_user_id=789,
            github_login="fixture-operator",
            user_id=actor.id,
        )
        mapped = await github_routes.map_github_actor(
            mapping_request,
            actor,
            db,
        )
        mapped = await github_routes.map_github_actor(mapping_request, actor, db)
        health = await github_routes.github_status(actor, db)
        assert mapped.github_user_id == 789
        assert link.archive_policy == "post_merge"
        assert health.repository_links[0].archive_policy == "post_merge"
        assert health.configured_apps[0].enabled is True
        project_row = await db.get(Project, project_id)
        assert project_row is not None
        repository_row = await db.get(Repository, repository_id)
        assert repository_row is not None
        original_target = repository_row.target_id
        repository_row.target_id = "not-an-owner-repository"
        with pytest.raises(HTTPException) as malformed_target:
            await github_routes.opt_in_github_repository(
                project_id,
                repository_id,
                opt_in,
                actor,
                db,
            )
        assert malformed_target.value.status_code == 409
        repository_row.target_id = original_target
        opted_out = await github_routes.opt_out_github_repository(
            project_id, repository_id, actor, db
        )
        assert opted_out.status_code == 204
        link = await github_routes.opt_in_github_repository(
            project_id, repository_id, opt_in, actor, db
        )
        assert link.opted_in is True
        app_id = app.id
        organization_id = project_row.organization_id
    issue_payload = {
        "installation": {"id": 456},
        "repository": {"id": 4321, "full_name": "fixture/specifications"},
        "sender": {"id": 789, "login": "fixture-operator"},
        "action": "opened",
        "issue": {
            "id": 888,
            "number": 19,
            "title": "Archive changes after verified merge",
            "body": "Complete the authorized issue workflow.",
            "html_url": "https://github.test/fixture/specifications/issues/19",
            "labels": [{"name": "harnessa"}],
        },
    }
    issue_body = json.dumps(issue_payload).encode()
    signature = (
        "sha256=" + hmac.new(b"fixture-webhook-secret", issue_body, hashlib.sha256).hexdigest()
    )

    def webhook_request(body: bytes = issue_body, declared_length: int | None = None) -> Request:
        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/github/webhook",
                "raw_path": b"/github/webhook",
                "query_string": b"",
                "headers": [(b"content-length", str(declared_length or len(body)).encode())],
                "client": ("127.0.0.1", 1234),
                "server": ("github.test", 80),
            },
            receive,
        )

    with pytest.raises(HTTPException) as invalid_signature:
        async with SessionLocal() as db:
            await github_routes.receive_github_webhook(
                webhook_request(),
                Response(),
                "123",
                "a888-888",
                "issues",
                "sha256=" + ("0" * 64),
                db,
            )
    assert invalid_signature.value.status_code == 401
    with pytest.raises(HTTPException) as invalid_hook_id:
        async with SessionLocal() as db:
            await github_routes.receive_github_webhook(
                webhook_request(), Response(), "invalid", "a888-889", "issues", signature, db
            )
    assert invalid_hook_id.value.status_code == 400
    with pytest.raises(HTTPException) as invalid_event_name:
        async with SessionLocal() as db:
            await github_routes.receive_github_webhook(
                webhook_request(), Response(), "123", "a888-889", "push!", signature, db
            )
    assert invalid_event_name.value.status_code == 400
    with pytest.raises(HTTPException) as unknown_hook:
        async with SessionLocal() as db:
            await github_routes.receive_github_webhook(
                webhook_request(), Response(), "124", "a888-889", "issues", signature, db
            )
    assert unknown_hook.value.status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(settings, "github_webhook_max_bytes", len(issue_body) - 1)
        with pytest.raises(HTTPException) as oversize_header:
            async with SessionLocal() as db:
                await github_routes.receive_github_webhook(
                    webhook_request(), Response(), "123", "a888-889", "issues", signature, db
                )
        assert oversize_header.value.status_code == 413
        with pytest.raises(HTTPException) as oversize_stream:
            async with SessionLocal() as db:
                await github_routes.receive_github_webhook(
                    webhook_request(declared_length=1),
                    Response(),
                    "123",
                    "a888-889",
                    "issues",
                    signature,
                    db,
                )
        assert oversize_stream.value.status_code == 413
    for malformed_body, status_code in ((b"not-json", 400), (b"[]", 400)):
        malformed_signature = (
            "sha256="
            + hmac.new(b"fixture-webhook-secret", malformed_body, hashlib.sha256).hexdigest()
        )
        with pytest.raises(HTTPException) as malformed_payload:
            async with SessionLocal() as db:
                await github_routes.receive_github_webhook(
                    webhook_request(malformed_body),
                    Response(),
                    "123",
                    f"c{len(malformed_body)}-889",
                    "issues",
                    malformed_signature,
                    db,
                )
        assert malformed_payload.value.status_code == status_code
    async with SessionLocal() as db:
        receipt = await github_routes.receive_github_webhook(
            webhook_request(),
            Response(),
            "123",
            "a888-888",
            "issues",
            signature,
            db,
        )
    assert receipt.duplicate is False
    duplicate_response = Response()
    async with SessionLocal() as db:
        duplicate = await github_routes.receive_github_webhook(
            webhook_request(),
            duplicate_response,
            "123",
            "a888-888",
            "issues",
            signature,
            db,
        )
    assert duplicate.duplicate is True
    assert duplicate_response.headers["X-Harnessa-Webhook-Duplicate"] == "true"
    changed_payload = json.dumps({"installation": {"id": 456}, "different": True}).encode()
    changed_signature = (
        "sha256=" + hmac.new(b"fixture-webhook-secret", changed_payload, hashlib.sha256).hexdigest()
    )
    with pytest.raises(HTTPException) as reused_delivery:
        async with SessionLocal() as db:
            await github_routes.receive_github_webhook(
                webhook_request(changed_payload),
                Response(),
                "123",
                "a888-888",
                "issues",
                changed_signature,
                db,
            )
    assert reused_delivery.value.status_code == 409
    processed = await process_github_webhook_outbox(
        GitHubWebhookProcessor(), owner_id="github-acceptance-fixture"
    )
    assert processed == 1
    unsupported_payload = json.dumps(
        {"installation": {"id": 456}, "repository": {"id": 4321}, "action": "completed"}
    ).encode()
    unsupported_signature = (
        "sha256="
        + hmac.new(b"fixture-webhook-secret", unsupported_payload, hashlib.sha256).hexdigest()
    )
    async with SessionLocal() as db:
        unsupported_receipt = await github_routes.receive_github_webhook(
            webhook_request(unsupported_payload),
            Response(),
            "123",
            "b888-889",
            "push",
            unsupported_signature,
            db,
        )
    assert unsupported_receipt.duplicate is False
    assert (
        await process_github_webhook_outbox(
            GitHubWebhookProcessor(), owner_id="github-acceptance-retry"
        )
        == 1
    )
    async with SessionLocal() as db:
        delivery = await db.scalar(
            select(GitHubWebhookDelivery).where(GitHubWebhookDelivery.delivery_id == "b888-889")
        )
        assert delivery is not None and delivery.status == "ignored"
        queued = await github_routes.retry_github_delivery(
            delivery.id,
            GitHubDeliveryRepairRequest(reason="The authorized repository setup is ready."),
            actor,
            db,
        )
        assert queued == {"status": "queued"}
        outbox = await db.scalar(
            select(GitHubWebhookOutbox).where(GitHubWebhookOutbox.delivery_id == delivery.id)
        )
        assert outbox is not None and outbox.status == "pending" and outbox.attempts == 0
        assert delivery.status == "pending" and delivery.result["repairs"]
        delivery.status = "failed"
        await db.delete(outbox)
        await db.commit()
        with pytest.raises(HTTPException) as missing_outbox:
            await github_routes.retry_github_delivery(
                delivery.id,
                GitHubDeliveryRepairRequest(reason="No durable processing intent exists."),
                actor,
                db,
            )
        assert missing_outbox.value.status_code == 409
    async with SessionLocal() as db:
        issue_sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.repository_link_id == link.id,
                GitHubSyncObject.object_kind == "issue",
                GitHubSyncObject.github_object_id == "888",
            )
        )
        assert issue_sync is not None and issue_sync.task_id is not None
        issue_interview = await db.scalar(
            select(SpecificationInterview).where(
                SpecificationInterview.task_id == issue_sync.task_id
            )
        )
        assert issue_interview is not None
        question = await db.scalar(
            select(SpecificationQuestion).where(
                SpecificationQuestion.interview_id == issue_interview.id
            )
        )
        assert question is not None
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as missing_publish_task:
            await github_routes.queue_github_review_publication(
                uuid4(), GitHubPublishRequest(idempotency_key="missing-task"), actor, db
            )
        assert missing_publish_task.value.status_code == 404
        with pytest.raises(HTTPException) as missing_issue_task:
            await github_routes.queue_github_issue_creation(uuid4(), actor, db)
        assert missing_issue_task.value.status_code == 404
        with pytest.raises(HTTPException) as missing_status_task:
            await github_routes.queue_github_commit_status(
                uuid4(),
                GitHubCommitStatusRequest(state="success", description="missing"),
                actor,
                db,
            )
        assert missing_status_task.value.status_code == 404
        with pytest.raises(HTTPException) as missing_delivery:
            await github_routes.retry_github_delivery(
                uuid4(), GitHubDeliveryRepairRequest(reason="Check the delivery."), actor, db
            )
        assert missing_delivery.value.status_code == 409

    issue_processor = GitHubWebhookProcessor()
    issue_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="issues",
        action="opened",
        installation_number=456,
        github_repository_id=4321,
        payload=issue_payload,
        delivery_id="issue-open-replay",
    )
    replay = await issue_processor.process(issue_delivery)
    assert replay.result["replayed"] is True
    for index, payload in enumerate(
        (
            {"repository": {"id": 4321}},
            {"repository": {"id": 4321}, "issue": {"id": 901, "number": 21}},
            {
                "repository": {"id": 4321},
                "sender": {"id": 789},
                "issue": {
                    "number": 21,
                    "title": "Missing issue ID",
                    "labels": [{"name": "harnessa"}],
                },
            },
            {
                "repository": {"id": 4321},
                "sender": {"id": 789},
                "issue": {
                    "id": 901,
                    "number": 21,
                    "title": "No label",
                    "labels": [{"name": "other"}],
                },
            },
            {
                "repository": {"id": 4321},
                "sender": {"id": 900},
                "issue": {
                    "id": 901,
                    "number": 21,
                    "title": "Unmapped sender",
                    "labels": [{"name": "harnessa"}],
                },
            },
            {
                "repository": {"id": 4321},
                "sender": {"id": 789},
                "issue": {"id": 901, "number": 21, "title": " ", "labels": [{"name": "harnessa"}]},
            },
        ),
        start=1,
    ):
        outcome = await issue_processor.process(
            DeliveryEnvelope(
                id=uuid4(),
                organization_id=organization_id,
                app_configuration_id=app_id,
                event_name="issues",
                action="opened",
                installation_number=456,
                github_repository_id=4321,
                payload=payload,
                delivery_id=f"issue-open-ignored-{index}",
            )
        )
        assert outcome.state == "ignored"

    answer_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="issue_comment",
        action="created",
        installation_number=456,
        github_repository_id=4321,
        payload={
            "installation": {"id": 456},
            "repository": {"id": 4321},
            "sender": {"id": 789, "login": "fixture-operator"},
            "issue": {"id": 888, "number": 19},
            "comment": {
                "id": 889,
                "body": f"/harnessa answer {question.id} r1: Keep the repository defaults.",
            },
        },
        delivery_id="authorized-answer-delivery",
    )
    answered = await issue_processor.process(answer_delivery)
    assert answered.result["question_id"] == str(question.id)
    for index, payload in enumerate(
        (
            {"repository": {"id": 4321}, "issue": {"id": 888}, "comment": {"body": "thanks"}},
            {
                "installation": {"id": 456},
                "repository": {"id": 4321},
                "sender": {"id": 900},
                "issue": {"id": 888},
                "comment": {"body": f"/harnessa answer {question.id} r1: Unauthorized."},
            },
            {
                **answer_delivery.payload,
                "comment": {
                    "id": 890,
                    "body": f"/harnessa answer {question.id} r1: Stale revision.",
                },
            },
        ),
        start=1,
    ):
        outcome = await issue_processor.process(
            DeliveryEnvelope(
                id=uuid4(),
                organization_id=organization_id,
                app_configuration_id=app_id,
                event_name="issue_comment",
                action="created",
                installation_number=456,
                github_repository_id=4321,
                payload=payload,
                delivery_id=f"issue-answer-ignored-{index}",
            )
        )
        assert outcome.state == "ignored"
    remote_issue_states = {
        "/repos/fixture/specifications/issues/19": {
            "id": 888,
            "number": 19,
            "title": issue_payload["issue"]["title"],
            "body": issue_payload["issue"]["body"],
            "html_url": issue_payload["issue"]["html_url"],
            "state": "open",
            "labels": [{"name": "harnessa"}],
        }
    }
    missed_webhook_issue = {
        "id": 903,
        "number": 22,
        "user": {"id": 789, "login": "fixture-operator"},
    }
    remote_issue_states["/repos/fixture/specifications/issues/22"] = {
        **missed_webhook_issue,
        "title": "Recovered without an opened webhook",
        "body": "This issue was found by the periodic topic scan.",
        "html_url": "https://github.test/fixture/specifications/issues/22",
        "state": "open",
        "labels": [{"name": "harnessa"}],
    }
    issue_scan_page_one = [
        missed_webhook_issue,
        {"id": 904, "number": 23, "user": {"id": 900, "login": "unmapped"}},
        {
            "id": 905,
            "number": 24,
            "user": {"id": 789, "login": "fixture-operator"},
            "pull_request": {"url": "https://api.github.test/pulls/24"},
        },
        {"id": 906, "number": 25, "user": "invalid"},
        {},
    ]
    issue_scan_result: object = issue_scan_page_one + [{}] * (100 - len(issue_scan_page_one))
    issue_scan_error: github_processor.GitHubAppError | None = None

    class CurrentIssueClient:
        async def request_installation(self, **kwargs: object) -> object:
            path = kwargs["path"]
            assert isinstance(path, str)
            if path == "/repos/fixture/specifications/issues":
                params = kwargs["params"]
                assert isinstance(params, dict)
                assert params["state"] == "open"
                assert params["labels"] == "harnessa"
                assert params["per_page"] == 100
                assert params["page"] in {1, 2}
                assert kwargs["permissions"] == {"issues": "read"}
                if issue_scan_error is not None:
                    raise issue_scan_error
                if params["page"] == 2:
                    return [missed_webhook_issue]
                return issue_scan_result
            assert kwargs["method"] == "GET"
            assert kwargs["permissions"] == {"issues": "read"}
            return remote_issue_states[path]

    monkeypatch.setattr(github_processor, "GitHubAppClient", CurrentIssueClient)
    # The issue has no inbox receipt and is recovered from the bounded issue scan.
    assert await issue_processor.reconcile_topic_issues(limit=1) == 1
    async with SessionLocal() as db:
        scan_link = await db.get(GitHubRepositoryLink, link.id)
        assert scan_link is not None and scan_link.issue_reconciliation_page == 2
        recovered_sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.object_kind == "issue",
                GitHubSyncObject.github_object_id == "903",
            )
        )
        assert recovered_sync is not None
        recovered_task_id = recovered_sync.task_id
        assert recovered_sync.field_baseline == {
            "title": "Recovered without an opened webhook",
            "body": "This issue was found by the periodic topic scan.",
        }
        assert await db.scalar(
            select(GitHubWebhookDelivery.id).where(
                GitHubWebhookDelivery.delivery_id == "reconcile-issue-4321-903-1"
            )
        ) is None
    # Replaying the same page is idempotent: it reuses the sync row and task.
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    async with SessionLocal() as db:
        scan_link = await db.get(GitHubRepositoryLink, link.id)
        assert scan_link is not None and scan_link.issue_reconciliation_page == 1
        replayed_sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.object_kind == "issue",
                GitHubSyncObject.github_object_id == "903",
            )
        )
        assert replayed_sync is not None and replayed_sync.task_id == recovered_task_id

    issue_scan_result = None
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    issue_scan_result = [
        {},
        {"id": "invalid", "number": 24, "user": {"id": 789}},
        {"id": 905, "number": 24, "pull_request": {}, "user": {"id": 789}},
        {"id": 906, "number": 25, "user": "invalid"},
    ]
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    issue_scan_result = [missed_webhook_issue]
    issue_scan_error = github_processor.GitHubAppError(
        "limited", rate_limited=True, retry_after_seconds=60
    )
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    async with SessionLocal() as db:
        scan_link = await db.get(GitHubRepositoryLink, link.id)
        assert scan_link is not None and scan_link.retry_after_at is not None
        scan_link.retry_after_at = None
        await db.commit()
    issue_scan_error = github_processor.GitHubAppError("forbidden", status_code=403)
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    async with SessionLocal() as db:
        scan_link = await db.get(GitHubRepositoryLink, link.id)
        assert scan_link is not None and scan_link.setup_status == "stale"
        scan_link.setup_status = "ready"
        scan_link.last_error = None
        await db.commit()
    issue_scan_error = github_processor.GitHubAppError("missing", status_code=404)
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    async with SessionLocal() as db:
        scan_link = await db.get(GitHubRepositoryLink, link.id)
        assert scan_link is not None and scan_link.opted_in is False
        scan_link.opted_in = True
        scan_link.setup_status = "ready"
        scan_link.last_error = None
        await db.commit()
    issue_scan_error = github_processor.GitHubAppError("transient")
    assert await issue_processor.reconcile_topic_issues(limit=1) == 0
    issue_scan_error = None
    issue_scan_result = [missed_webhook_issue]
    unchanged = await issue_processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="issues",
            action="edited",
            installation_number=456,
            github_repository_id=4321,
            payload={
                "repository": {"id": 4321},
                "issue": {
                    "id": 888,
                    "title": issue_payload["issue"]["title"],
                    "body": issue_payload["issue"]["body"],
                },
            },
            delivery_id="issue-edit-unchanged",
        )
    )
    assert unchanged.result["unchanged"] is True
    untracked_edit = await issue_processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="issues",
            action="edited",
            installation_number=456,
            github_repository_id=4321,
            payload={"repository": {"id": 4321}, "issue": {"id": 902, "number": 21}},
            delivery_id="issue-edit-untracked",
        )
    )
    assert untracked_edit.result["reason"] == "sender_not_mapped"
    current_issue = {
        "id": 902,
        "number": 21,
        "title": "Current title after an early edit",
        "body": "Authoritative current issue body.",
        "html_url": "https://github.test/fixture/specifications/issues/21",
        "state": "open",
        "labels": [{"name": "harnessa"}],
    }
    remote_issue_states["/repos/fixture/specifications/issues/21"] = current_issue
    reordered_edit_payload = {
        "action": "edited",
        "installation": {"id": 456},
        "repository": {"id": 4321, "full_name": "fixture/specifications"},
        "sender": {"id": 789},
        "issue": {
            "id": 902,
            "number": 21,
            "title": "Stale edit delivery title",
            "body": "Stale edit delivery body.",
        },
    }
    reordered_edit_body = json.dumps(reordered_edit_payload).encode()
    reordered_edit_signature = (
        "sha256="
        + hmac.new(b"fixture-webhook-secret", reordered_edit_body, hashlib.sha256).hexdigest()
    )
    async with SessionLocal() as db:
        reordered_edit_receipt = await github_routes.receive_github_webhook(
            webhook_request(reordered_edit_body),
            Response(),
            "123",
            "a902-0001",
            "issues",
            reordered_edit_signature,
            db,
        )
    assert reordered_edit_receipt.duplicate is False
    assert await process_github_webhook_outbox(
        GitHubWebhookProcessor(), owner_id="github-reordered-edit-first"
    ) == 1
    async with SessionLocal() as db:
        reordered_edit_delivery = await db.scalar(
            select(GitHubWebhookDelivery).where(
                GitHubWebhookDelivery.delivery_id == "a902-0001"
            )
        )
        assert reordered_edit_delivery is not None
        assert reordered_edit_delivery.status == "processed"
        assert reordered_edit_delivery.result["created_from_current_state"] is True
    stale_open_payload = {
        "action": "opened",
        "installation": {"id": 456},
        "repository": {"id": 4321, "full_name": "fixture/specifications"},
        "sender": {"id": 789},
        "issue": {
            "id": 902,
            "number": 21,
            "title": "Older open payload title",
            "body": "Older open payload body.",
            "html_url": current_issue["html_url"],
            "labels": [{"name": "harnessa"}],
        },
    }
    stale_open_body = json.dumps(stale_open_payload).encode()
    stale_open_signature = (
        "sha256="
        + hmac.new(b"fixture-webhook-secret", stale_open_body, hashlib.sha256).hexdigest()
    )
    async with SessionLocal() as db:
        stale_open_receipt = await github_routes.receive_github_webhook(
            webhook_request(stale_open_body),
            Response(),
            "123",
            "a902-0002",
            "issues",
            stale_open_signature,
            db,
        )
    assert stale_open_receipt.duplicate is False
    assert await process_github_webhook_outbox(
        GitHubWebhookProcessor(), owner_id="github-open-after-edit"
    ) == 1
    async with SessionLocal() as db:
        reordered_sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.repository_link_id == link.id,
                GitHubSyncObject.object_kind == "issue",
                GitHubSyncObject.github_object_id == "902",
            )
        )
        assert reordered_sync is not None
        assert reordered_sync.field_baseline == {
            "title": current_issue["title"],
            "body": current_issue["body"],
        }
        interview = await db.scalar(
            select(SpecificationInterview).where(
                SpecificationInterview.task_id == reordered_sync.task_id
            )
        )
        assert interview is not None
        assert interview.topic == (
            "Current title after an early edit\n\nAuthoritative current issue body."
        )
    edit_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="issues",
        action="edited",
        installation_number=456,
        github_repository_id=4321,
        payload={
            "installation": {"id": 456},
            "repository": {"id": 4321},
            "issue": {"id": 888, "title": issue_payload["issue"]["title"], "body": "human edit"},
        },
        delivery_id="issue-edit-delivery",
    )
    issue_888 = remote_issue_states["/repos/fixture/specifications/issues/19"]
    assert isinstance(issue_888, dict)
    issue_888["body"] = "human edit"
    edited = await issue_processor.process(edit_delivery)
    assert edited.result["conflict_fields"] == ["body"]

    interview_id = await _create_ready_interview(
        project_id,
        repository_id,
        actor,
        idempotency_key="github-issue:456:888",
        expect_replayed=True,
        topic=("Archive changes after verified merge\n\nComplete the authorized issue workflow."),
    )
    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        assert interview is not None
        with pytest.raises(HTTPException) as missing_pull_request:
            await github_routes.queue_github_commit_status(
                interview.task_id,
                GitHubCommitStatusRequest(state="success", description="No PR yet"),
                actor,
                db,
            )
        assert missing_pull_request.value.status_code == 409

    source = await manager.source_path(repository)
    archive_path = "openspec/changes/archive/2026-10-23-example/change.md"
    implementation_path = "src/accepted.py"
    (source / archive_path).parent.mkdir(parents=True, exist_ok=True)
    (source / archive_path).write_text("Archived specification change.\n", encoding="utf-8")
    (source / implementation_path).parent.mkdir(parents=True, exist_ok=True)
    (source / implementation_path).write_text(
        "def accepted() -> bool:\n    return True\n", encoding="utf-8"
    )
    _git(source, "add", archive_path, implementation_path)
    _git(
        source,
        "-c",
        "user.email=fixture@example.test",
        "-c",
        "user.name=Fixture",
        "commit",
        "--quiet",
        "-m",
        "archive candidate",
    )
    candidate_head = _git(source, "rev-parse", "HEAD")
    candidate_ref = f"refs/harnessa/review-candidates/{uuid4().hex}-f1"
    _git(source, "update-ref", candidate_ref, candidate_head)
    candidate_base = _git(source, "rev-parse", f"{candidate_head}^")
    _git(source, "update-ref", "refs/heads/main", candidate_base)
    archive_diff = (
        f"diff --git a/{archive_path} b/{archive_path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{archive_path}\n"
        "@@ -0,0 +1 @@\n"
        "+Archived specification change.\n"
    )
    archive_digest = hashlib.sha256(archive_diff.encode()).hexdigest()

    async with SessionLocal() as db:
        interview = await db.get(SpecificationInterview, interview_id)
        repository_row = await db.get(Repository, repository_id)
        policy = await db.scalar(
            select(ModelBudgetPolicy).where(ModelBudgetPolicy.project_id == project_id)
        )
        assert interview is not None and repository_row is not None and policy is not None
        task_id = interview.task_id
        task = await db.get(Task, task_id)
        assert task is not None
        run = Run(
            task_id=task.id,
            project_id=project_id,
            attempt=1,
            status="SUCCEEDED",
            fencing_token=1,
            adapter_name="github-fixture",
            source_revision=interview.base_sha,
            base_sha=interview.base_sha,
            specification_hash=interview.artifact_hash,
            specification_revision=interview.artifact_revision,
            specification_commit_sha=interview.head_sha,
            policy_revision=interview.policy_revision,
            budget_policy_digest=budget_policy_digest(policy),
            harness_revision=settings.acp_harness_revision,
            harness_digest=hashlib.sha256(b"").hexdigest(),
            agent_image=settings.acp_agent_image,
        )
        db.add(run)
        await db.flush()
        manifest = await db.scalar(
            select(VerificationManifestRevision).where(
                VerificationManifestRevision.repository_id == repository_id
            )
        )
        assert manifest is not None
        execution = VerificationExecution(
            organization_id=interview.organization_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            run_id=run.id,
            manifest_revision_id=manifest.id,
            attempt=1,
            fencing_token=1,
            source_revision=candidate_head,
            outcome="passed",
            result={"criteria": [{"id": "archive", "outcome": "passed"}]},
            content_hash=hashlib.sha256(b"archive-fixture-evidence").hexdigest(),
        )
        db.add(execution)
        await db.flush()
        bundle = ReviewBundle(
            organization_id=interview.organization_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            run_id=run.id,
            verification_execution_id=execution.id,
            fencing_token=1,
            base_sha=interview.base_sha,
            head_sha=candidate_head,
            specification_hash=interview.artifact_hash,
            specification_revision=interview.artifact_revision,
            harness_revision=settings.acp_harness_revision,
            manifest_digest=manifest.manifest_digest,
            bundle_digest=hashlib.sha256(b"archive-maintenance-bundle").hexdigest(),
            artifact_digest=hashlib.sha256(b"archive-maintenance-artifact").hexdigest(),
            bundle={
                "implementation_head_sha": interview.base_sha,
                "openspec_sync_preview": {
                    "archive_preview_ready": True,
                    "final_head_verified": True,
                    "implementation_head_sha": interview.base_sha,
                    "proposed_final_review_head": candidate_head,
                    "archive_diff": archive_diff,
                    "diff_sha256": archive_digest,
                    "review_candidate_ref": candidate_ref,
                },
            },
            status="prepared",
        )
        db.add(bundle)
        await db.flush()
        base_sha = interview.base_sha
        stale_reason = await review_routes._bundle_stale_reason(db, bundle)
        assert stale_reason is None, stale_reason
        task.stage = "HUMAN_REVIEW"
        task.run_status = "SUCCEEDED"
        task.version += 1
        await db.commit()
        publication = await github_routes.queue_github_review_publication(
            task_id,
            GitHubPublishRequest(idempotency_key="github-acceptance-publish"),
            actor,
            db,
        )
        assert publication.queued is True
        first_intent = await db.scalar(
            select(EffectIntent).where(
                EffectIntent.stable_key == f"github-review-publish:{bundle.id}"
            )
        )
        assert first_intent is not None
        await db.delete(first_intent)
        bundle.status = "published"
        db.add_all(
            [
                ReviewDecision(
                    organization_id=interview.organization_id,
                    project_id=project_id,
                    task_id=task_id,
                    bundle_id=bundle.id,
                    actor_id=actor.id,
                    decision="approve",
                    rationale="A mismatched bundle digest must not authorize publication.",
                    idempotency_key="github-acceptance-wrong-digest",
                    bundle_digest=hashlib.sha256(b"different bundle").hexdigest(),
                    head_sha=bundle.head_sha,
                ),
                ReviewDecision(
                    organization_id=interview.organization_id,
                    project_id=project_id,
                    task_id=task_id,
                    bundle_id=bundle.id,
                    actor_id=actor.id,
                    decision="approve",
                    rationale="A mismatched head must not authorize publication.",
                    idempotency_key="github-acceptance-wrong-head",
                    bundle_digest=bundle.bundle_digest,
                    head_sha="e" * 40,
                ),
            ]
        )
        await db.flush()
        assert await has_current_review_approval(db, bundle) is False
        db.add(
            ReviewDecision(
                organization_id=interview.organization_id,
                project_id=project_id,
                task_id=task_id,
                bundle_id=bundle.id,
                actor_id=actor.id,
                decision="approve",
                rationale="The exact verified bundle is ready for GitHub publication.",
                idempotency_key="github-acceptance-review-approval",
                bundle_digest=bundle.bundle_digest,
                head_sha=bundle.head_sha,
            )
        )
        await db.flush()
        assert await has_current_review_approval(db, bundle) is True
        await db.commit()
        publication = await github_routes.queue_github_review_publication(
            task_id,
            GitHubPublishRequest(idempotency_key="github-acceptance-publish-after-approval"),
            actor,
            db,
        )
        assert publication.queued is True
        bundle_id = bundle.id

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    branch_sha = "d" * 40
    tree_sha = "e" * 40
    merge_sha = "f" * 40
    review_branch_sha = "b" * 40
    review_tree_sha = "c" * 40
    review_merge_sha = "a" * 40
    bundle_digest = hashlib.sha256(b"archive-maintenance-bundle").hexdigest()
    archive_integration_id: UUID | None = None
    review_merged = False
    review_head_override: str | None = None
    review_base_override: str | None = None
    check_conclusion = "success"
    installation_gone = False
    installation_rate_limited = False
    repository_access_removed = False
    repository_rate_limited = False
    pull_request_rate_limited = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal archive_integration_id
        body_bytes = await request.aread()
        body = json.loads(body_bytes) if body_bytes else None
        calls.append((request.method, request.url.path, body if isinstance(body, dict) else None))
        path = request.url.path
        if path == "/app/installations/456/access_tokens":
            assert isinstance(body, dict)
            return httpx.Response(
                201,
                json={
                    "token": "fixture-installation-token",
                    "permissions": body["permissions"],
                    "repositories": [{"id": 4321}],
                },
            )
        if path == "/app/installations/456":
            if installation_rate_limited:
                return httpx.Response(
                    429, headers={"Retry-After": "60"}, json={"message": "limited"}
                )
            if installation_gone:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={"permissions": APP_REQUIRED_PERMISSIONS, "suspended_at": None},
            )
        if path == "/app/installations/457":
            return httpx.Response(
                200,
                json={"permissions": APP_REQUIRED_PERMISSIONS, "suspended_at": None},
            )
        if path == "/repos/fixture/specifications":
            if repository_rate_limited:
                return httpx.Response(
                    403,
                    headers={"Retry-After": "45"},
                    json={"message": "secondary rate limit"},
                )
            if repository_access_removed:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": 4321, "default_branch": "main"})
        if path == "/repos/fixture/specifications/git/ref/heads/main":
            default_sha = review_merge_sha if review_merged else base_sha
            return httpx.Response(200, json={"object": {"sha": default_sha}})
        if path.startswith("/repos/fixture/specifications/compare/"):
            comparison = path.rsplit("/", 1)[-1]
            left_sha, right_sha = comparison.split("...", maxsplit=1)
            return httpx.Response(
                200,
                json={
                    "status": "identical" if left_sha == right_sha else "ahead",
                    "merge_base_commit": {"sha": left_sha},
                },
            )
        if path == "/repos/fixture/specifications/pulls" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path in {
            "/repos/fixture/specifications/issues/19/comments",
            "/repos/fixture/specifications/issues/21/comments",
            "/repos/fixture/specifications/issues/22/comments",
        }:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            assert request.method == "POST" and body is not None
            return httpx.Response(
                201,
                json={
                    "id": 889,
                    "html_url": "https://github.test/fixture/specifications/comments/889",
                },
            )
        if path == "/repos/fixture/specifications/issues" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/repos/fixture/specifications/issues" and request.method == "POST":
            assert body is not None
            return httpx.Response(
                201,
                json={
                    "id": 890,
                    "number": 20,
                    "state": "open",
                    "body": body["body"],
                    "html_url": "https://github.test/fixture/specifications/issues/20",
                },
            )
        if (
            path == f"/repos/fixture/specifications/commits/{branch_sha}/statuses"
            and request.method == "GET"
        ):
            return httpx.Response(200, json=[])
        if path in {
            "/repos/fixture/specifications/pulls/18",
            "/repos/fixture/specifications/pulls/19",
            "/repos/fixture/specifications/pulls/20",
        }:
            if path.endswith("/18") and pull_request_rate_limited:
                return httpx.Response(
                    403,
                    headers={"X-RateLimit-Remaining": "0", "Retry-After": "30"},
                    json={"message": "rate limited"},
                )
            is_review = path.endswith("/18")
            is_check_fixture = path.endswith("/20")
            pull_number = 18 if is_review else (20 if is_check_fixture else 19)
            pull_id = 700 if is_review else (702 if is_check_fixture else 701)
            head_sha = (review_head_override or review_branch_sha) if is_review else branch_sha
            base_sha_value = (review_base_override or base_sha) if is_review else base_sha
            merged = review_merged if is_review else not is_check_fixture
            return httpx.Response(
                200,
                json={
                    "id": pull_id,
                    "number": pull_number,
                    "state": "closed" if merged else "open",
                    "merged": merged,
                    "merge_commit_sha": review_merge_sha if is_review else merge_sha,
                    "html_url": f"https://github.test/fixture/specifications/pull/{pull_number}",
                    "head": {"sha": head_sha},
                    "base": {"sha": base_sha_value},
                },
            )
        if path == "/repos/fixture/specifications/pulls/18/reviews":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 799,
                        "state": "APPROVED",
                        "commit_id": review_branch_sha,
                        "body": "Approved",
                        "user": {"id": 789},
                    }
                ],
            )
        if path in {
            "/repos/fixture/specifications/pulls/19/reviews",
            "/repos/fixture/specifications/pulls/20/reviews",
        }:
            return httpx.Response(200, json=[])
        if path in {
            f"/repos/fixture/specifications/commits/{review_branch_sha}/check-runs",
            f"/repos/fixture/specifications/commits/{branch_sha}/check-runs",
        }:
            head_sha = review_branch_sha if review_branch_sha in path else branch_sha
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 9001,
                            "name": "Local CI",
                            "head_sha": head_sha,
                            "status": "completed",
                            "conclusion": check_conclusion,
                        }
                    ]
                },
            )
        if (
            path.startswith("/repos/fixture/specifications/git/ref/heads/harnessa/")
            and request.method == "GET"
        ):
            return httpx.Response(404, json={"message": "Not Found"})
        if path in {
            f"/repos/fixture/specifications/git/commits/{base_sha}",
            f"/repos/fixture/specifications/git/commits/{review_merge_sha}",
        }:
            return httpx.Response(200, json={"tree": {"sha": "f" * 40}})
        if path == "/repos/fixture/specifications/git/blobs":
            assert body is not None and body["encoding"] == "base64"
            return httpx.Response(201, json={"sha": "a" * 40})
        if path == "/repos/fixture/specifications/git/trees":
            assert body is not None
            if body["tree"]:
                assert len(body["tree"]) == 1
                entry = body["tree"][0]
                assert entry["mode"] == "100644" and entry["type"] == "blob"
                assert entry["sha"] == "a" * 40
                if entry["path"] == archive_path:
                    return httpx.Response(201, json={"sha": tree_sha})
                assert entry["path"] == implementation_path
                return httpx.Response(201, json={"sha": review_tree_sha})
            return httpx.Response(201, json={"sha": tree_sha})
        if path == "/repos/fixture/specifications/git/commits":
            assert body is not None
            if f"Harnessa review bundle {bundle_digest}" in body["message"]:
                return httpx.Response(201, json={"sha": review_branch_sha})
            assert archive_digest in body["message"]
            import re

            marker = re.search(r"Harnessa-Archive: ([0-9a-f-]{36})", body["message"])
            assert marker is not None
            archive_integration_id = UUID(marker.group(1))
            return httpx.Response(201, json={"sha": branch_sha})
        if path == "/repos/fixture/specifications/git/refs":
            assert body is not None
            return httpx.Response(201, json={"object": {"sha": body["sha"]}})
        if path == f"/repos/fixture/specifications/git/commits/{review_branch_sha}":
            return httpx.Response(
                200,
                json={"message": f"Review\n\nHarnessa-Bundle: {bundle_digest}"},
            )
        if path == f"/repos/fixture/specifications/git/commits/{branch_sha}":
            assert archive_integration_id is not None
            return httpx.Response(
                200,
                json={
                    "message": f"Archive\n\n<!-- Harnessa-Archive: {archive_integration_id} {archive_digest} -->"
                },
            )
        if path == "/repos/fixture/specifications/pulls" and request.method == "POST":
            assert body is not None
            if bundle_digest in body["body"]:
                return httpx.Response(
                    201,
                    json={
                        "id": 700,
                        "number": 18,
                        "html_url": "https://github.test/fixture/specifications/pull/18",
                        "head": {"sha": review_branch_sha},
                        "base": {"sha": base_sha},
                    },
                )
            assert archive_digest in body["body"]
            return httpx.Response(
                201,
                json={
                    "id": 701,
                    "number": 19,
                    "html_url": "https://github.test/fixture/specifications/pull/19",
                    "head": {"sha": branch_sha},
                },
            )
        if (
            path == f"/repos/fixture/specifications/statuses/{branch_sha}"
            and request.method == "POST"
        ):
            assert body is not None
            return httpx.Response(
                201,
                json={"id": 991, "context": body["context"], "state": body["state"]},
            )
        return httpx.Response(500, json={"message": f"Unexpected {request.method} {path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        github_effects,
        "GitHubAppClient",
        lambda: GitHubAppClient(
            api_url="https://api.github.test",
            transport=transport,
            references_json='{"app-key":"GITHUB_TEST_KEY"}',
            environ={"GITHUB_TEST_KEY": private_pem},
        ),
    )
    monkeypatch.setattr(
        github_processor,
        "GitHubAppClient",
        lambda: GitHubAppClient(
            api_url="https://api.github.test",
            transport=transport,
            references_json='{"app-key":"GITHUB_TEST_KEY"}',
            environ={"GITHUB_TEST_KEY": private_pem},
        ),
    )
    assert await GitHubWebhookProcessor().reconcile_installation_access() == 1
    effects_processed = await reconcile_effect_intents(
        github_effects.GitHubEffectReconciler(workspace_manager=manager)
    )
    assert effects_processed == 5
    async with SessionLocal() as db:
        effect_intents = (await db.scalars(select(EffectIntent))).all()
        assert len(effect_intents) == 5
        assert all(intent.status == "completed" for intent in effect_intents)
        review_sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.repository_link_id == link.id,
                GitHubSyncObject.object_number == 18,
            )
        )
        published_bundle = await db.get(ReviewBundle, bundle_id)
        assert review_sync is not None and review_sync.head_sha == review_branch_sha, [
            intent.result for intent in effect_intents
        ]
        assert published_bundle is not None and published_bundle.status == "published"
    processor = GitHubWebhookProcessor()
    assert await processor.reconcile_tracked_pull_requests() == 1
    stale_pr = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request",
            action="opened",
            installation_number=456,
            github_repository_id=4321,
            payload={"number": 18, "pull_request": {"id": 999}},
            delivery_id="pull-request-identity-mismatch",
        )
    )
    assert stale_pr.result["reason"] == "remote_pull_request_identity_mismatch"
    untracked_pr = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request",
            action="opened",
            installation_number=456,
            github_repository_id=4321,
            payload={"number": 999, "pull_request": {"id": 999}},
            delivery_id="pull-request-untracked",
        )
    )
    assert untracked_pr.result["reason"] == "untracked_pull_request"
    unauthorized_review = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request_review",
            action="submitted",
            installation_number=456,
            github_repository_id=4321,
            payload={
                "number": 18,
                "pull_request": {"id": 700},
                "review": {"id": 800, "state": "APPROVED"},
                "sender": {"id": 900},
            },
            delivery_id="pull-request-review-unmapped",
        )
    )
    assert unauthorized_review.result["reason"] == "reviewer_not_mapped"
    for index, record in enumerate(
        (None, {"head_sha": "invalid"}, {"head_sha": branch_sha, "pull_requests": []})
    ):
        invalid_check = await processor.process(
            DeliveryEnvelope(
                id=uuid4(),
                organization_id=organization_id,
                app_configuration_id=app_id,
                event_name="check_run",
                action="completed",
                installation_number=456,
                github_repository_id=4321,
                payload={"check_run": record} if record is not None else {},
                delivery_id=f"invalid-check-{index}",
            )
        )
        assert invalid_check.state == "ignored"
    async with SessionLocal() as db:
        approval = await db.scalar(
            select(ReviewDecision).where(
                ReviewDecision.bundle_id == bundle_id,
                ReviewDecision.decision == "approve",
                ReviewDecision.github_head_sha == review_branch_sha,
            )
        )
        assert approval is not None
    for action, review_id in (("submitted", 799), ("dismissed", 799)):
        current_review = await processor.process(
            DeliveryEnvelope(
                id=uuid4(),
                organization_id=organization_id,
                app_configuration_id=app_id,
                event_name="pull_request_review",
                action=action,
                installation_number=456,
                github_repository_id=4321,
                payload={
                    "number": 18,
                    "pull_request": {"id": 700},
                    "review": {"id": review_id, "state": "APPROVED"},
                    "sender": {"id": 789},
                },
                delivery_id=f"pull-request-review-{action}",
            )
        )
        assert current_review.state == "processed"

    check_conclusion = "failure"
    failed_task_id = uuid4()
    async with SessionLocal() as db:
        failed_task = Task(
            id=failed_task_id,
            project_id=project_id,
            title="Required check remediation fixture",
            stage="HUMAN_REVIEW",
            run_status="SUCCEEDED",
            version=1,
        )
        db.add(failed_task)
        await db.flush()
        db.add(
            GitHubSyncObject(
                organization_id=organization_id,
                project_id=project_id,
                repository_link_id=link.id,
                task_id=failed_task_id,
                object_kind="pull_request",
                github_object_id="702",
                object_number=20,
                stable_key="github-check-remediation-fixture",
                state="open",
                base_sha=base_sha,
                head_sha=branch_sha,
                check_results={},
                field_baseline={},
                sync_conflicts={},
            )
        )
        await db.commit()
    failed_check = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="check_run",
            action="completed",
            installation_number=456,
            github_repository_id=4321,
            payload={
                "check_run": {
                    "head_sha": branch_sha,
                    "pull_requests": [{"id": 702, "number": 20}],
                }
            },
            delivery_id="required-check-failure",
        )
    )
    assert failed_check.state == "processed"
    async with SessionLocal() as db:
        failed_task = await db.get(Task, failed_task_id)
        assert failed_task is not None and failed_task.stage == "READY"
        assert failed_task.blocked_reason == "github_required_check_failed"
    check_conclusion = "success"
    review_merged = True
    review_merge_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="pull_request",
        action="closed",
        installation_number=456,
        github_repository_id=4321,
        payload={
            "installation": {"id": 456},
            "repository": {"id": 4321},
            "number": 18,
            "pull_request": {"id": 700},
        },
        delivery_id="review-merge-delivery",
    )
    review_merged_result = await processor.process(review_merge_delivery)
    assert review_merged_result.result["state"] == "merged"
    integration_value = review_merged_result.result["integration_id"]
    assert isinstance(integration_value, str)
    integration_id = UUID(integration_value)
    processed = await process_post_integration_steps(workspace_manager=manager)
    assert processed == 3
    async with SessionLocal() as db:
        step = await db.scalar(
            select(PostIntegrationStep).where(
                PostIntegrationStep.integration_evidence_id == integration_id,
                PostIntegrationStep.kind == "archive",
            )
        )
        assert step is not None
        result = step.result

    assert result["outcome"] == "awaiting_merge"
    assert result["pull_request_number"] == 19
    assert result["archive_diff_sha256"] == archive_digest
    assert any(method == "POST" and path.endswith("/pulls") for method, path, _ in calls)
    async with SessionLocal() as db:
        issue_intent = await github_routes.queue_github_issue_creation(task_id, actor, db)
        status_intent = await github_routes.queue_github_commit_status(
            task_id,
            GitHubCommitStatusRequest(
                state="success",
                description="Acceptance fixture completed",
                target_url="https://github.test/fixture/result",
            ),
            actor,
            db,
        )
        issue_replay = await github_routes.queue_github_issue_creation(task_id, actor, db)
        status_replay = await github_routes.queue_github_commit_status(
            task_id,
            GitHubCommitStatusRequest(
                state="success",
                description="Acceptance fixture completed",
                target_url="https://github.test/fixture/result",
            ),
            actor,
            db,
        )
        assert issue_intent.queued is True and status_intent.queued is True
        assert issue_replay.stable_keys == issue_intent.stable_keys
        assert status_replay.stable_keys == status_intent.stable_keys
        integration_read = await review_routes.get_integration_evidence(task_id, actor, db)
        archive_read = next(step for step in integration_read.steps if step.kind == "archive")
        assert archive_read.status == "awaiting_merge"
        assert archive_read.result["pull_request_number"] == 19
        sync = await db.scalar(
            select(GitHubSyncObject).where(
                GitHubSyncObject.stable_key
                == f"github-archive-pr:{integration_id}:{archive_digest}"
            )
        )
        assert sync is not None
        assert sync.object_purpose == "maintenance_archive"
        assert sync.object_number == 19
        assert sync.head_sha == branch_sha
    outbound_effects = await reconcile_effect_intents(
        github_effects.GitHubEffectReconciler(workspace_manager=manager)
    )
    assert outbound_effects == 2
    async with SessionLocal() as db:
        effects = (await db.scalars(select(EffectIntent))).all()
        assert len(effects) == 7
        assert all(effect.status == "completed" for effect in effects)
    assert any(
        method == "POST" and path == "/repos/fixture/specifications/issues"
        for method, path, _ in calls
    )
    assert any(
        method == "POST" and path.endswith(f"/statuses/{branch_sha}") for method, path, _ in calls
    ), [(method, path) for method, path, _ in calls if "status" in path]
    check_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="check_run",
        action="completed",
        installation_number=456,
        github_repository_id=4321,
        payload={
            "installation": {"id": 456},
            "repository": {"id": 4321},
            "check_run": {
                "head_sha": branch_sha,
                "pull_requests": [{"id": 701, "number": 19}],
            },
        },
        delivery_id="archive-check-delivery",
    )
    checked = await processor.process(check_delivery)
    assert checked.result["checks"] == [{"pull_number": 19, "all_required_passed": True}]
    merge_delivery = DeliveryEnvelope(
        id=uuid4(),
        organization_id=organization_id,
        app_configuration_id=app_id,
        event_name="pull_request",
        action="closed",
        installation_number=456,
        github_repository_id=4321,
        payload={
            "installation": {"id": 456},
            "repository": {"id": 4321},
            "number": 19,
            "pull_request": {"id": 701},
        },
        delivery_id="archive-merge-delivery",
    )
    merged = await processor.process(merge_delivery)
    assert merged.result["state"] == "merged"
    async with SessionLocal() as db:
        archived = await db.scalar(
            select(PostIntegrationStep).where(
                PostIntegrationStep.integration_evidence_id == integration_id,
                PostIntegrationStep.kind == "archive",
            )
        )
        task = await db.get(Task, task_id)
        assert archived is not None and archived.status == "succeeded"
        assert archived.result["maintenance_merge_commit_sha"] == merge_sha
        assert task is not None and task.stage == "DONE"
    review_head_override = "9" * 40
    unverified_merge = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request",
            action="closed",
            installation_number=456,
            github_repository_id=4321,
            payload={"number": 18, "pull_request": {"id": 700}},
            delivery_id="merged-review-head-drift",
        )
    )
    assert unverified_merge.result["state"] == "merged_unverified"
    review_merged = False
    head_drift = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request",
            action="synchronize",
            installation_number=456,
            github_repository_id=4321,
            payload={"number": 18, "pull_request": {"id": 700}},
            delivery_id="open-review-head-drift",
        )
    )
    assert head_drift.result["state"] == "head_drift"
    review_head_override = review_branch_sha
    review_base_override = "8" * 40
    base_drift = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="pull_request",
            action="edited",
            installation_number=456,
            github_repository_id=4321,
            payload={"number": 18, "pull_request": {"id": 700}},
            delivery_id="open-review-base-drift",
        )
    )
    assert base_drift.result["state"] == "base_drift"
    repository_access_removed = True

    pull_request_rate_limited = True
    before_pr_rate_limit = len(calls)
    await processor.reconcile_tracked_pull_requests()
    assert len(calls) > before_pr_rate_limit
    async with SessionLocal() as db:
        rate_limited_review_sync = await db.get(GitHubSyncObject, review_sync.id)
        assert rate_limited_review_sync is not None
        assert rate_limited_review_sync.retry_after_at is not None
        assert rate_limited_review_sync.retry_after_at > datetime.now(UTC)
    review_request_count = sum(
        method == "GET" and path.endswith("/pulls/18") for method, path, _ in calls
    )
    await processor.reconcile_tracked_pull_requests()
    assert (
        sum(method == "GET" and path.endswith("/pulls/18") for method, path, _ in calls)
        == review_request_count
    )
    pull_request_rate_limited = False
    async with SessionLocal() as db:
        rate_limited_review_sync = await db.get(GitHubSyncObject, review_sync.id)
        assert rate_limited_review_sync is not None
        rate_limited_review_sync.retry_after_at = None
        await db.commit()

    repository_access_removed = False
    installation_rate_limited = True
    before_installation_rate_limit = len(calls)
    assert await processor.reconcile_installation_access() == 0
    assert len(calls) > before_installation_rate_limit
    async with SessionLocal() as db:
        rate_limited_installation = await db.get(GitHubInstallation, installation.id)
        assert rate_limited_installation is not None
        assert rate_limited_installation.retry_after_at is not None
        assert rate_limited_installation.retry_after_at > datetime.now(UTC)
    installation_request_count = sum(
        method == "GET" and path == "/app/installations/456" for method, path, _ in calls
    )
    assert await processor.reconcile_installation_access() == 0
    assert (
        sum(method == "GET" and path == "/app/installations/456" for method, path, _ in calls)
        == installation_request_count
    )
    installation_rate_limited = False
    async with SessionLocal() as db:
        rate_limited_installation = await db.get(GitHubInstallation, installation.id)
        assert rate_limited_installation is not None
        rate_limited_installation.retry_after_at = None
        await db.commit()

    repository_rate_limited = True
    before_repository_rate_limit = len(calls)
    assert await processor.reconcile_installation_access() == 0
    assert len(calls) > before_repository_rate_limit
    async with SessionLocal() as db:
        rate_limited_link = await db.get(GitHubRepositoryLink, link.id)
        assert rate_limited_link is not None
        assert rate_limited_link.retry_after_at is not None
        assert rate_limited_link.retry_after_at > datetime.now(UTC)
    repository_call_count = sum(
        method == "GET" and path == "/repos/fixture/specifications" for method, path, _ in calls
    )
    assert await processor.reconcile_installation_access() == 0
    assert (
        sum(
            method == "GET" and path == "/repos/fixture/specifications" for method, path, _ in calls
        )
        == repository_call_count
    )
    repository_rate_limited = False
    async with SessionLocal() as db:
        rate_limited_link = await db.get(GitHubRepositoryLink, link.id)
        assert rate_limited_link is not None
        rate_limited_link.retry_after_at = None
        await db.commit()

    repository_access_removed = True
    assert await processor.reconcile_installation_access() == 1
    async with SessionLocal() as db:
        revoked_link = await db.get(GitHubRepositoryLink, link.id)
        assert revoked_link is not None
        assert revoked_link.opted_in is False and revoked_link.setup_status == "revoked"
        revoked_link.opted_in = True
        revoked_link.setup_status = "ready"
        revoked_link.last_error = None
        await db.commit()
    repository_access_removed = False
    installation_gone = True
    assert await processor.reconcile_installation_access() == 1
    async with SessionLocal() as db:
        missing_installation_row = await db.get(GitHubInstallation, installation.id)
        unavailable_link = await db.get(GitHubRepositoryLink, link.id)
        assert missing_installation_row is not None and missing_installation_row.active is False
        assert unavailable_link is not None and unavailable_link.opted_in is False
    installation_gone = False
    unsuspended = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="installation",
            action="unsuspend",
            installation_number=456,
            github_repository_id=None,
            payload={"installation": {"id": 456}},
            delivery_id="installation-unsuspend-delivery",
        )
    )
    assert unsuspended.state == "processed"
    repository_removed = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="installation_repositories",
            action="removed",
            installation_number=456,
            github_repository_id=None,
            payload={"repositories_removed": [{"id": 4321}, {"id": "invalid"}]},
            delivery_id="installation-repository-removal-delivery",
        )
    )
    assert repository_removed.state == "processed"
    removed = await processor.process(
        DeliveryEnvelope(
            id=uuid4(),
            organization_id=organization_id,
            app_configuration_id=app_id,
            event_name="installation",
            action="deleted",
            installation_number=456,
            github_repository_id=None,
            payload={"installation": {"id": 456}},
            delivery_id="installation-removal-delivery",
        )
    )
    assert removed.result["installation_active"] is False
    async with SessionLocal() as db:
        refreshed_link = await db.get(GitHubRepositoryLink, link.id)
        assert refreshed_link is not None and refreshed_link.setup_status == "revoked"
        organization_membership = await db.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == actor.id,
            )
        )
        assert organization_membership is not None
        organization_membership.role = "operator"
        await db.commit()
        operator_status = await github_routes.github_status(actor, db)
        assert operator_status.configured_apps == []
        assert operator_status.installations == []
        assert operator_status.webhook_health == {}
