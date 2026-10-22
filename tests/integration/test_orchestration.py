from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.execution import ExecutionFailure, StaleLease
from harnessa.config import settings
from harnessa.contracts import RunStatus, TaskCommand, TaskStage
from harnessa.db.models import Membership, Organization, Project, ProjectMembership, Task, User
from harnessa.db.orchestration import (
    ConsumerInbox,
    DeadLetter,
    DomainEvent,
    EffectIntent,
    OutboxMessage,
    Run,
    RunArtifact,
    TaskTransition,
)
from harnessa.db.session import SessionLocal
from harnessa.main import app
from harnessa.services.effects import reconcile_effect_intents
from harnessa.services.lifecycle import command_task, create_task, transition_task
from harnessa.services.recovery import replay_dead_letter
from harnessa.services.scheduler import Scheduler, retry_delay_seconds
from harnessa.services.streams import (
    CONSUMER_GROUP,
    STREAM_NAME,
    OutboxPublisher,
    StreamConsumer,
    rebuild_dispatch_stream,
)
from pwdlib import PasswordHash
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from tests.adapters.fake_execution import FakeExecutionAdapter

pytestmark = pytest.mark.integration


async def seed_project(role: str = "administrator") -> tuple[UUID, UUID, UUID]:
    async with SessionLocal() as db:
        organization = Organization(name="Integration Org")
        user = User(
            email=f"{role}@example.test",
            hashed_password=PasswordHash.recommended().hash("test-password-long-enough"),
            is_active=True,
            is_superuser=(role == "administrator"),
            is_verified=True,
        )
        db.add_all([organization, user])
        await db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
        project = Project(organization_id=organization.id, name="Test Project", slug="test-project")
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=user.id, role=role))
        await db.commit()
        return organization.id, project.id, user.id


async def ready_task(project_id: UUID, actor_id: UUID, *, key: str = "create-task") -> UUID:
    async with SessionLocal() as db:
        actor = await db.get(User, actor_id)
        task, _ = await create_task(
            db, project_id=project_id, actor=actor, title="Run fixture",
            description="Deterministic test input", priority=1, command_key=key,
        )
        task_id = task.id
    async with SessionLocal() as db:
        actor = await db.get(User, actor_id)
        await transition_task(
            db, task_id=task_id, actor=actor, target=TaskStage.SPECIFYING,
            expected_version=1, reason="Integration setup", return_stage=None,
            command_key=f"{key}-specifying",
        )
    async with SessionLocal() as db:
        actor = await db.get(User, actor_id)
        await transition_task(
            db, task_id=task_id, actor=actor, target=TaskStage.READY,
            expected_version=2, reason="Fixture is ready", return_stage=None,
            command_key=f"{key}-ready",
        )
    return task_id


async def row_count(model) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.anyio
async def test_concurrent_claim_and_expired_worker_fencing_with_checkpoint():
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    scheduler_a = Scheduler(global_limit=4, lease_seconds=5, heartbeat_seconds=1)
    scheduler_b = Scheduler(global_limit=4, lease_seconds=5, heartbeat_seconds=1)
    claims = await asyncio.gather(
        scheduler_a.claim("worker-a", "test-fake-execution"),
        scheduler_b.claim("worker-b", "test-fake-execution"),
    )
    jobs = [job for job in claims if job is not None]
    assert len(jobs) == 1
    old_job = jobs[0]
    await scheduler_a.save_checkpoint(old_job, {"step": "after-write", "nonce": "persisted"})
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE task_leases SET expires_at = :expired WHERE task_id = :task_id"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "task_id": task_id},
        )
        await db.commit()
    assert await scheduler_a.recover_expired_leases() == 1
    new_job = await scheduler_b.claim("worker-b", "test-fake-execution")
    assert new_job is not None
    assert new_job.fencing_token == old_job.fencing_token + 1
    assert new_job.checkpoint == {"step": "after-write", "nonce": "persisted"}
    with pytest.raises(StaleLease):
        await scheduler_a.complete(old_job, {"stale": True})
    assert await scheduler_b.heartbeat(new_job)


@pytest.mark.anyio
async def test_artifact_and_progress_publication_are_fenced_and_orphans_reconcile(tmp_path):
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    artifact_root = tmp_path / "artifacts"
    scheduler = Scheduler(
        global_limit=1,
        artifact_store=LocalArtifactStore(artifact_root),
    )
    job = await scheduler.claim("artifact-worker", "test-fake-execution")
    assert job is not None

    await scheduler.save_checkpoint(
        job,
        {"phase": "session_update", "update_counts": {"agent_message_chunk": 100}},
    )
    accepted = await scheduler.publish_artifact(
        job, b"accepted artifact", "application/octet-stream"
    )
    assert accepted["fencing_token"] == job.fencing_token
    assert accepted["run_id"] == str(job.run_id)

    async with SessionLocal() as db:
        stored = await db.scalar(select(RunArtifact).where(RunArtifact.run_id == job.run_id))
        events = (
            await db.execute(
                select(DomainEvent).where(DomainEvent.run_id == job.run_id)
            )
        ).scalars().all()
        assert stored is not None
        progress = next(event for event in events if event.event_type == "run.progress")
        publication = next(
            event for event in events if event.event_type == "run.artifact_published"
        )
        assert progress.payload == {
            "phase": "agent_updates",
            "update_counts": {"agent_message_chunk": 100},
        }
        assert "fixture private thought" not in str(progress.payload)
        assert publication.payload["digest"] == accepted["digest"]

    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE task_leases SET expires_at = :expired WHERE task_id = :task_id"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "task_id": task_id},
        )
        await db.commit()
    with pytest.raises(StaleLease):
        await scheduler.publish_artifact(job, b"stale artifact", "application/octet-stream")

    orphan = scheduler.artifact_store.put_bytes(
        b"unreferenced blob", media_type="application/octet-stream"
    )
    os.utime(artifact_root / orphan.storage_key, (0, 0))
    assert await scheduler.reconcile_orphan_artifacts(minimum_age_seconds=0) == 1
    assert await scheduler.is_current_fence(job.run_id, job.fencing_token) is False


@pytest.mark.anyio
async def test_expired_worker_without_checkpoint_fails_visibly_without_relaunch():
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    scheduler = Scheduler(global_limit=2, lease_seconds=1)
    job = await scheduler.claim("worker-a", "test-fake-execution")
    assert job
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE task_leases SET expires_at = :expired WHERE task_id = :task_id"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "task_id": task_id},
        )
        await db.commit()
    assert await scheduler.recover_expired_leases() == 1
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task.run_status == RunStatus.FAILED.value
        assert task.blocked_reason == "worker_lost_unrecoverable"
    assert await scheduler.claim("worker-b", "test-fake-execution") is None
    assert await row_count(DeadLetter) == 1


@pytest.mark.anyio
async def test_priority_and_project_concurrency_are_enforced_during_atomic_claims():
    _, project_id, actor_id = await seed_project()
    first_task = await ready_task(project_id, actor_id, key="low-priority")
    second_task = await ready_task(project_id, actor_id, key="high-priority")
    async with SessionLocal() as db:
        low = await db.get(Task, first_task)
        high = await db.get(Task, second_task)
        low.priority = 0
        high.priority = 100
        project = await db.get(Project, project_id)
        project.max_concurrency = 1
        await db.commit()

    scheduler = Scheduler(global_limit=1)
    claims = await asyncio.gather(
        scheduler.claim("priority-worker-a", "test-fake-execution"),
        scheduler.claim("priority-worker-b", "test-fake-execution"),
    )
    jobs = [job for job in claims if job is not None]
    assert len(jobs) == 1
    assert jobs[0].task_id == second_task


@pytest.mark.anyio
async def test_wall_time_limit_stops_retry_and_creates_dead_letter():
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    scheduler = Scheduler(global_limit=1, max_attempts=3, max_wall_seconds=1)
    job = await scheduler.claim("slow-worker", "test-fake-execution")
    assert job is not None
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE runs SET started_at = :started WHERE id = :run_id"),
            {"started": datetime.now(UTC) - timedelta(seconds=5), "run_id": job.run_id},
        )
        await db.commit()
    await scheduler.fail(
        job, ExecutionFailure("transient", "slow_run", "Injected over-time failure")
    )
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        dead = await db.scalar(select(DeadLetter).where(DeadLetter.task_id == task_id))
        assert task.stage == TaskStage.RETRY_EXHAUSTED.value
        assert task.blocked_reason == "wall_time_limit_exhausted"
        assert dead.kind == "slow_run"


@pytest.mark.anyio
async def test_active_cancel_is_forwarded_to_adapter_and_audited():
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    scheduler = Scheduler(
        global_limit=1, lease_seconds=5, heartbeat_seconds=0.05, max_attempts=2
    )
    adapter = FakeExecutionAdapter(mode="wait_for_cancel")
    running = asyncio.create_task(scheduler.run_once(adapter, "cancel-worker"))
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        actor = await db.get(User, actor_id)
        cancelled, _ = await command_task(
            db, task_id=task_id, actor=actor, command=TaskCommand.CANCEL,
            expected_version=task.version, reason="Operator stopped active execution",
            command_key="cancel-active-run",
        )
        assert cancelled.stage == TaskStage.CANCELLED.value
        assert cancelled.run_status == RunStatus.CANCELLED.value
    assert await asyncio.wait_for(running, timeout=2)
    assert adapter.cancelled
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        run = await db.scalar(select(Run).where(Run.task_id == task_id))
        transitions = (
            await db.execute(select(TaskTransition).where(TaskTransition.task_id == task_id))
        ).scalars().all()
        assert task.stage == TaskStage.CANCELLED.value
        assert run.status == RunStatus.CANCELLED.value
        assert any(item.command == "cancel" for item in transitions)


@pytest.mark.anyio
async def test_retry_exhaustion_cancellation_commands_and_operator_replay():
    _, project_id, actor_id = await seed_project()
    task_id = await ready_task(project_id, actor_id)
    scheduler = Scheduler(global_limit=2, max_attempts=1, rng=lambda: 0.5)
    assert await scheduler.run_once(FakeExecutionAdapter(mode="transient_failure"), "worker-a")
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        assert task.stage == TaskStage.RETRY_EXHAUSTED.value
        assert task.run_status == RunStatus.FAILED.value
        dead = await db.scalar(select(DeadLetter).where(DeadLetter.task_id == task_id))
        actor = await db.get(User, actor_id)
        recovered, replayed = await replay_dead_letter(
            db, dead_letter_id=dead.id, actor=actor,
            reason="Operator reviewed failure and requested recovery",
            command_key="replay-dead-letter",
        )
        assert not replayed
        assert recovered.stage == TaskStage.READY.value
        assert recovered.run_status == RunStatus.QUEUED.value
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        actor = await db.get(User, actor_id)
        paused, _ = await command_task(
            db, task_id=task_id, actor=actor, command=TaskCommand.PAUSE,
            expected_version=task.version, reason="Pause during recovery test",
            command_key="pause-recovered",
        )
        assert paused.run_status == RunStatus.PAUSED.value
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        actor = await db.get(User, actor_id)
        resumed, _ = await command_task(
            db, task_id=task_id, actor=actor, command=TaskCommand.RESUME,
            expected_version=task.version, reason="Resume test", command_key="resume-recovered",
        )
        assert resumed.run_status == RunStatus.QUEUED.value
    assert retry_delay_seconds(3, "rate_limited", retry_after_seconds=7) == 7
    assert retry_delay_seconds(3, "transient", random_value=0.5) == 4.0


@pytest.mark.anyio
async def test_duplicate_commands_outbox_crash_ack_dedup_and_redis_rebuild(monkeypatch):
    _, project_id, actor_id = await seed_project()
    async with SessionLocal() as db:
        actor = await db.get(User, actor_id)
        task, replayed = await create_task(
            db, project_id=project_id, actor=actor, title="One command",
            description="Deduplicate", priority=0, command_key="same-create-key",
        )
        task_id = task.id
        assert not replayed
    async with SessionLocal() as db:
        actor = await db.get(User, actor_id)
        task, replayed = await create_task(
            db, project_id=project_id, actor=actor, title="One command",
            description="Deduplicate", priority=0, command_key="same-create-key",
        )
        assert replayed
        assert task.id == task_id
    await ready_task(project_id, actor_id, key="ready-for-rebuild")
    await ready_task(project_id, actor_id, key="ready-for-outbox")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    original_xadd = redis.xadd
    did_crash = False

    async def crash_before_publish(*args, **kwargs):
        raise RuntimeError("injected crash after DB commit, before Redis publish")

    monkeypatch.setattr(redis, "xadd", crash_before_publish)
    publisher = OutboxPublisher(redis, owner_id="integration-publisher")
    assert await publisher.publish_once(limit=1) == 0
    assert await redis.xlen(STREAM_NAME) == 0
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE outbox_messages SET lease_until = :expired, next_attempt_at = NULL"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1)},
        )
        await db.commit()

    async def publish_then_crash(*args, **kwargs):
        nonlocal did_crash
        result = await original_xadd(*args, **kwargs)
        if not did_crash:
            did_crash = True
            raise RuntimeError("injected crash after Redis publish")
        return result

    monkeypatch.setattr(redis, "xadd", publish_then_crash)
    assert await publisher.publish_once(limit=1) == 0
    assert await redis.xlen(STREAM_NAME) == 1
    async with SessionLocal() as db:
        await db.execute(
            text("UPDATE outbox_messages SET lease_until = :expired, next_attempt_at = NULL"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1)},
        )
        await db.commit()
    monkeypatch.setattr(redis, "xadd", original_xadd)
    assert await publisher.publish_once(limit=1) == 1
    assert await redis.xlen(STREAM_NAME) == 2

    consumer = StreamConsumer(redis, consumer_name="integration-consumer", min_idle_ms=0)
    with pytest.raises(RuntimeError, match="Injected crash"):
        await consumer.process_once(count=10, block_ms=10, crash_after_commit=True)
    receipts_after_crash = await row_count(ConsumerInbox)
    assert receipts_after_crash == 1
    # Pending message is reclaimed; transactional inbox dedup suppresses a second effect.
    await consumer.process_once(count=10, block_ms=10)
    await consumer.process_once(count=10, block_ms=10)
    assert await row_count(ConsumerInbox) == receipts_after_crash
    pending = await redis.xpending(STREAM_NAME, CONSUMER_GROUP)
    assert pending["pending"] == 0

    await redis.delete(STREAM_NAME)
    rebuilt = await rebuild_dispatch_stream(redis)
    assert rebuilt == 2
    assert await redis.xlen(STREAM_NAME) == 2
    assert await row_count(DomainEvent) >= await row_count(TaskTransition)
    assert await row_count(OutboxMessage) == await row_count(DomainEvent)
    await redis.aclose()


@pytest.mark.anyio
async def test_external_effect_reconciliation_is_idempotent_and_retryable():
    _, project_id, _ = await seed_project()
    async with SessionLocal() as db:
        intent = EffectIntent(
            project_id=project_id,
            kind="repository_write",
            stable_key="task:effect:test",
            payload={"path": "spec.md", "content_hash": "abc"},
        )
        db.add(intent)
        await db.commit()
        intent_id = intent.id
        abandoned = EffectIntent(
            project_id=project_id,
            kind="repository_write",
            stable_key="task:effect:abandoned",
            payload={"path": "docs.md"},
            status="reconciling",
            attempts=1,
            updated_at=datetime.now(UTC) - timedelta(seconds=61),
        )
        db.add(abandoned)
        await db.commit()
        abandoned_id = abandoned.id

    class FakeReconciler:
        def __init__(self) -> None:
            self.outcomes = {
                "task:effect:test": ["not_applied", "applied"],
                "task:effect:abandoned": ["not_applied"],
            }

        async def reconcile(self, *, kind, stable_key, payload):
            assert kind == "repository_write"
            assert payload["path"] in {"spec.md", "docs.md"}
            return self.outcomes[stable_key].pop(0)

    adapter = FakeReconciler()
    assert await reconcile_effect_intents(adapter) == 2
    async with SessionLocal() as db:
        intent = await db.get(EffectIntent, intent_id)
        assert intent.status == "pending"
        intent.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await reconcile_effect_intents(adapter) == 1
    async with SessionLocal() as db:
        intent = await db.get(EffectIntent, intent_id)
        assert intent.status == "completed"
        assert intent.attempts == 2
        abandoned = await db.get(EffectIntent, abandoned_id)
        assert abandoned.status == "pending"
        assert abandoned.attempts == 2


@pytest.mark.anyio
async def test_api_task_contracts_authorization_and_project_cursor_replay():
    organization_id, project_id, _actor_id = await seed_project()
    # Add a same-organization operator who has no project grant.
    async with SessionLocal() as db:
        outsider = User(
            email="outsider@example.test",
            hashed_password=PasswordHash.recommended().hash("another-test-password-long"),
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(outsider)
        await db.flush()
        db.add(Membership(organization_id=organization_id, user_id=outsider.id, role="operator"))
        outsider_email = outsider.email
        await db.commit()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            origin = settings.allowed_origins.split(",")[0].strip()
            csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            login = await client.post(
                "/auth/cookie/login",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                data={"username": "administrator@example.test", "password": "test-password-long-enough"},
            )
            assert login.status_code == 204
            write_headers = {
                "Origin": origin,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "api-create-task",
            }
            created = await client.post(
                f"/projects/{project_id}/tasks",
                headers=write_headers,
                json={"title": "API task", "description": "contract test", "priority": 3},
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]
            duplicate = await client.post(
                f"/projects/{project_id}/tasks",
                headers=write_headers,
                json={"title": "API task", "description": "contract test", "priority": 3},
            )
            assert duplicate.status_code == 200
            assert duplicate.headers["Idempotency-Replayed"] == "true"
            illegal = await client.post(
                f"/tasks/{task_id}/transition",
                headers={**write_headers, "Idempotency-Key": "api-illegal", "If-Match": '"1"'},
                json={"target_stage": "IN_PROGRESS", "reason": "Attempt to bypass scheduler"},
            )
            assert illegal.status_code == 409
            assert illegal.json()["code"] == "transition_rejected"
            transitioned = await client.post(
                f"/tasks/{task_id}/transition",
                headers={**write_headers, "Idempotency-Key": "api-transition", "If-Match": '"1"'},
                json={"target_stage": "SPECIFYING", "reason": "Start spec"},
            )
            assert transitioned.status_code == 200, transitioned.text
            stale = await client.post(
                f"/tasks/{task_id}/transition",
                headers={**write_headers, "Idempotency-Key": "api-stale", "If-Match": '"1"'},
                json={"target_stage": "READY", "reason": "Stale version"},
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "version_conflict"
            page = await client.get(f"/projects/{project_id}/events?after=0&limit=10")
            assert page.status_code == 200
            assert [item["cursor"] for item in page.json()["items"]] == sorted(
                item["cursor"] for item in page.json()["items"]
            )
            history = await client.get(f"/tasks/{task_id}/transitions")
            assert history.status_code == 200 and len(history.json()) == 2
            run_history = await client.get(f"/tasks/{task_id}/runs")
            assert run_history.status_code == 200 and run_history.json() == []

            await client.post("/auth/logout", headers={"Origin": origin, "X-CSRF-Token": csrf})
            unauthenticated = await client.get(f"/tasks/{task_id}")
            assert unauthenticated.status_code == 401
            outsider_csrf = (await client.get("/auth/csrf")).json()["csrf_token"]
            outsider_login = await client.post(
                "/auth/cookie/login",
                headers={"Origin": origin, "X-CSRF-Token": outsider_csrf},
                data={"username": outsider_email, "password": "another-test-password-long"},
            )
            assert outsider_login.status_code == 204
            hidden = await client.get(f"/tasks/{task_id}")
            assert hidden.status_code == 403
            another_project_tasks = await client.get(f"/projects/{project_id}/tasks")
            assert another_project_tasks.status_code == 403
            hidden_events = await client.get(f"/projects/{project_id}/events?after=0")
            assert hidden_events.status_code == 403
