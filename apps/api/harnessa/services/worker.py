"""Worker process for outbox delivery, event handling, and fenced scheduling."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from uuid import uuid4

from redis.asyncio import Redis

from harnessa.adapters.execution import UnconfiguredExecutionAdapter
from harnessa.config import settings
from harnessa.services.effects import ReconcileOutcome, reconcile_effect_intents
from harnessa.services.scheduler import Scheduler
from harnessa.services.streams import OutboxPublisher, StreamConsumer, prune_retained_events

logger = logging.getLogger("harnessa.worker")


async def run() -> None:
    """Run dispatch and recovery loops; no fake or simulated production executor exists."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
    publisher = OutboxPublisher(redis, owner_id=owner)
    consumer = StreamConsumer(redis, consumer_name=owner)
    scheduler = Scheduler()
    adapter = UnconfiguredExecutionAdapter()
    active_runs: set[asyncio.Task[bool]] = set()
    last_prune = 0.0
    await consumer.ensure_group()
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
                await publisher.publish_once()
                await consumer.process_once(block_ms=100)
                await scheduler.recover_expired_leases()
                await reconcile_effect_intents(_UnconfiguredEffectReconciler())
                if adapter.available:
                    available = max(0, settings.max_concurrent_runs - len(active_runs))
                    for _ in range(available):
                        active_runs.add(asyncio.create_task(scheduler.run_once(adapter, owner)))
                now = asyncio.get_running_loop().time()
                if now - last_prune >= 3600:
                    await prune_retained_events()
                    last_prune = now
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
        if active_runs:
            await asyncio.gather(*active_runs, return_exceptions=True)
        await redis.aclose()


class _UnconfiguredEffectReconciler:
    """Keep effect intents retryable until a provider adapter is configured."""

    async def reconcile(
        self, *, kind: str, stable_key: str, payload: Mapping[str, object]
    ) -> ReconcileOutcome:
        return "unknown"
