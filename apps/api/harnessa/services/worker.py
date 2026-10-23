"""Worker process for outbox delivery, event handling, and fenced scheduling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from harnessa.adapters.acp_execution import ACPExecutionAdapter
from harnessa.adapters.artifacts import LocalArtifactStore
from harnessa.adapters.execution import ExecutionAdapter, UnconfiguredExecutionAdapter
from harnessa.adapters.github_effects import GitHubEffectReconciler
from harnessa.adapters.protocols import HarnessEvaluator
from harnessa.adapters.sandbox_policy import SandboxLimits
from harnessa.config import settings
from harnessa.db.session import SessionLocal
from harnessa.services.effects import EffectResult, ReconcileOutcome, reconcile_effect_intents
from harnessa.services.evidence import reconcile_due_gateway_usage_imports
from harnessa.services.github_processor import GitHubWebhookProcessor
from harnessa.services.github_webhooks import process_github_webhook_outbox
from harnessa.services.harness_experiments import HarnessExperimentWorker
from harnessa.services.model_gateway import reconcile_stale_model_reservations
from harnessa.services.post_integration import process_post_integration_steps
from harnessa.services.scheduler import Scheduler
from harnessa.services.streams import OutboxPublisher, StreamConsumer, prune_retained_events
from harnessa.services.verification_workflow import VerificationDispatcher
from redis.asyncio import Redis

logger = logging.getLogger("harnessa.worker")


def _configured_execution_adapter() -> ExecutionAdapter:
    """Build only the local ACP runner; a brokered model route is still required."""
    if not settings.acp_execution_enabled:
        return UnconfiguredExecutionAdapter()
    try:
        decoded = json.loads(settings.acp_repository_sources_json)
    except json.JSONDecodeError as exc:
        raise ValueError("ACP_REPOSITORY_SOURCES_JSON must be valid JSON") from exc
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in decoded.items()
    ):
        raise ValueError("ACP_REPOSITORY_SOURCES_JSON must map project ids to source paths")
    limits = SandboxLimits(
        cpus=settings.acp_cpu_limit,
        memory_bytes=settings.acp_memory_limit_bytes,
        pids=settings.acp_pids_limit,
        tmpfs_bytes=settings.acp_tmpfs_limit_bytes,
        workspace_bytes=settings.acp_workspace_limit_bytes,
        wall_seconds=settings.run_max_wall_seconds,
        max_protocol_frame_bytes=settings.acp_frame_limit_bytes,
        max_agent_updates=settings.acp_update_limit,
        max_artifact_bytes=settings.acp_artifact_limit_bytes,
    )
    return ACPExecutionAdapter(
        enabled=True,
        repository_sources={key: Path(value) for key, value in decoded.items()},
        workspace_root=Path(settings.acp_workspace_root),
        state_root=Path(settings.acp_runner_state_root),
        artifact_store=LocalArtifactStore(Path(settings.artifact_root)),
        image=settings.acp_agent_image,
        limits=limits,
        # The run-scoped bridge sends every model request through Harnessa budgets.
        # The agent receives only its run token and cannot reach provider networks.
        model_route_ready=(settings.acp_model_broker_enabled and bool(settings.model_gateway_url)),
        model_alias=settings.acp_model_alias,
        model_max_output_tokens=settings.acp_model_max_output_tokens,
    )


async def run(*, harness_evaluator: HarnessEvaluator | None = None) -> None:
    """Run dispatch and recovery loops; no fake or simulated production executor exists."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
    publisher = OutboxPublisher(redis, owner_id=owner)
    consumer = StreamConsumer(redis, consumer_name=owner)
    scheduler = Scheduler()
    adapter = _configured_execution_adapter()
    harness_execution = adapter if isinstance(adapter, ACPExecutionAdapter) else None
    harness_worker = HarnessExperimentWorker(
        harness_execution, worker_id=owner, evaluator=harness_evaluator
    )

    async def current_execution_fence(run_id: UUID, fencing_token: int) -> bool:
        return await scheduler.is_current_fence(run_id, fencing_token) or await harness_worker.is_current_fence(
            run_id, fencing_token
        )
    try:
        source_mapping = json.loads(settings.acp_repository_sources_json)
        verification_dispatcher = VerificationDispatcher(
            {str(key): Path(value) for key, value in source_mapping.items()}
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        verification_dispatcher = None
    active_runs: set[asyncio.Task[bool]] = set()
    active_harness: asyncio.Task[bool] | None = None
    last_harness_dispatch = 0.0
    last_prune = 0.0
    last_model_reconcile = 0.0
    last_gateway_usage_reconcile = 0.0
    last_acp_reconcile = 0.0
    last_github_reconcile = 0.0
    last_post_integration_reconcile = 0.0
    await consumer.ensure_group()
    if isinstance(adapter, ACPExecutionAdapter) and adapter.enabled:
        try:
            await adapter.validate()
            await adapter.reconcile_orphans(is_current_fence=current_execution_fence)
            await scheduler.reconcile_orphan_artifacts()
            last_acp_reconcile = asyncio.get_running_loop().time()
            logger.info(
                "Rootless ACP sandbox validated; ACP dispatch readiness=%s",
                adapter.available,
            )
        except Exception:
            logger.exception("ACP startup validation or orphan reconciliation failed")
    try:
        while True:
            try:
                completed = {task for task in active_runs if task.done()}
                active_runs.difference_update(completed)
                for task in completed:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Run worker failed outside its recovery boundary")
                if active_harness is not None and active_harness.done():
                    try:
                        active_harness.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Harness experiment worker failed outside its recovery boundary")
                    active_harness = None
                await publisher.publish_once()
                await consumer.process_once(block_ms=100)
                await scheduler.recover_expired_leases()
                if verification_dispatcher is not None:
                    try:
                        await verification_dispatcher.run_once()
                    except Exception:
                        logger.exception(
                            "Independent verifier failed; durable task remains recoverable"
                        )
                await reconcile_effect_intents(_UnconfiguredEffectReconciler())
                await process_github_webhook_outbox(GitHubWebhookProcessor(), owner_id=owner)
                now = asyncio.get_running_loop().time()
                if now - last_post_integration_reconcile >= 30:
                    try:
                        await process_post_integration_steps(limit=20)
                    except Exception:
                        logger.exception("Post-integration finalization failed")
                    last_post_integration_reconcile = now
                if now - last_github_reconcile >= 300:
                    try:
                        github_processor = GitHubWebhookProcessor()
                        await github_processor.reconcile_installation_access(limit=25)
                        await github_processor.reconcile_tracked_pull_requests(limit=25)
                        await github_processor.reconcile_topic_issues(limit=25)
                    except Exception:
                        logger.exception("GitHub periodic reconciliation failed")
                    last_github_reconcile = now
                if adapter.available:
                    available = max(0, settings.max_concurrent_runs - len(active_runs))
                    for _ in range(available):
                        active_runs.add(asyncio.create_task(scheduler.run_once(adapter, owner)))
                if active_harness is None and asyncio.get_running_loop().time() - last_harness_dispatch >= 1:
                    active_harness = asyncio.create_task(harness_worker.run_once())
                    last_harness_dispatch = asyncio.get_running_loop().time()
                now = asyncio.get_running_loop().time()
                if now - last_model_reconcile >= 30:
                    await reconcile_stale_model_reservations(SessionLocal)
                    last_model_reconcile = now
                if now - last_gateway_usage_reconcile >= 30:
                    try:
                        await reconcile_due_gateway_usage_imports(SessionLocal, limit=100)
                    except Exception:
                        logger.exception("Late gateway usage reconciliation failed")
                    last_gateway_usage_reconcile = now
                if now - last_prune >= 3600:
                    await prune_retained_events()
                    await scheduler.reconcile_orphan_artifacts()
                    last_prune = now
                if isinstance(adapter, ACPExecutionAdapter) and now - last_acp_reconcile >= 30:
                    last_acp_reconcile = now
                    try:
                        if not adapter.sandbox_validated:
                            await adapter.validate()
                        await adapter.reconcile_orphans(is_current_fence=current_execution_fence)
                    except Exception:
                        logger.exception("ACP periodic validation or orphan reconciliation failed")
                if not active_runs:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker orchestration loop failed; retrying")
                await asyncio.sleep(1)
    finally:
        for task in active_runs:
            task.cancel()
        if active_harness is not None:
            active_harness.cancel()
        pending_workers = [*active_runs]
        if active_harness is not None:
            pending_workers.append(active_harness)
        if pending_workers:
            await asyncio.gather(*pending_workers, return_exceptions=True)
        await redis.aclose()


class _UnconfiguredEffectReconciler:
    """Keep effect intents retryable until a provider adapter is configured."""

    async def reconcile(
        self, *, kind: str, stable_key: str, payload: Mapping[str, object]
    ) -> ReconcileOutcome | EffectResult:
        if kind.startswith("github."):
            return await GitHubEffectReconciler().reconcile(
                kind=kind, stable_key=stable_key, payload=payload
            )
        return "unknown"
