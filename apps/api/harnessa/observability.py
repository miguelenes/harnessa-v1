"""Bounded-cardinality HTTP and durable operational metrics."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harnessa.db.github import GitHubWebhookOutbox
from harnessa.db.model_gateway import (
    AliasDeployment,
    ModelAlias,
    ModelDeployment,
    ModelPool,
    ModelReservation,
    PoolAlias,
    ProviderAccount,
)
from harnessa.db.orchestration import (
    DeadLetter,
    EffectIntent,
    OutboxMessage,
    Run,
    RunArtifact,
    TaskLease,
)
from harnessa.db.review import PostIntegrationStep
from harnessa.db.session import SessionLocal

_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_KNOWN_METHODS = frozenset({"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"})


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class RequestMetrics:
    """Process-local request counters and latency histogram using route templates."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str, int], float] = defaultdict(float)
        self._buckets: dict[tuple[str, str, int], list[int]] = {}

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        # ASGI accepts arbitrary method tokens. Collapse extensions to a stable label
        # so an unauthenticated caller cannot grow the process-local metric maps.
        method = method if method in _KNOWN_METHODS else "OTHER"
        key = (method, route, status)
        with self._lock:
            self._counts[key] += 1
            self._duration_sum[key] += max(0.0, duration_seconds)
            buckets = self._buckets.setdefault(key, [0] * len(_BUCKETS))
            for index, boundary in enumerate(_BUCKETS):
                if duration_seconds <= boundary:
                    buckets[index] += 1

    def render(self) -> str:
        lines = [
            "# HELP harnessa_http_requests_total Total HTTP requests handled by this API process.",
            "# TYPE harnessa_http_requests_total counter",
            "# HELP harnessa_http_request_duration_seconds HTTP request latency in seconds.",
            "# TYPE harnessa_http_request_duration_seconds histogram",
        ]
        with self._lock:
            for (method, route, status), count in sorted(self._counts.items()):
                labels = f'method="{_escape_label(method)}",route="{_escape_label(route)}",status="{status}"'
                lines.append(f"harnessa_http_requests_total{{{labels}}} {count}")
                buckets = self._buckets[(method, route, status)]
                for boundary, bucket_count in zip(_BUCKETS, buckets, strict=True):
                    lines.append(
                        f'harnessa_http_request_duration_seconds_bucket{{{labels},le="{boundary}"}} {bucket_count}'
                    )
                lines.append(
                    f'harnessa_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
                )
                lines.append(
                    f"harnessa_http_request_duration_seconds_sum{{{labels}}} {self._duration_sum[(method, route, status)]:.9f}"
                )
                lines.append(f"harnessa_http_request_duration_seconds_count{{{labels}}} {count}")
        return "\n".join(lines) + "\n"


request_metrics = RequestMetrics()


class OperationalMetrics:
    """Short-lived database snapshot for release-critical operational health."""

    _CACHE_SECONDS = 5.0

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
        self._session_factory = session_factory
        self._lock = asyncio.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, float] = {}
        self._logger = logging.getLogger("harnessa.observability")

    async def snapshot(self) -> dict[str, float]:
        async with self._lock:
            if self._cached and monotonic() - self._cached_at < self._CACHE_SECONDS:
                return {**self._cached, "collection_success": 1.0}
            try:
                values = await self._collect()
            except SQLAlchemyError:  # Database diagnostics must not take down metrics scraping.
                self._logger.warning("operational metrics collection failed")
                return {"collection_success": 0.0}
            self._cached = values
            self._cached_at = monotonic()
            return {**values, "collection_success": 1.0}

    async def _collect(self) -> dict[str, float]:
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            queued_count, queued_oldest = await self._count_oldest(
                db, Run, Run.status == "QUEUED"
            )
            stale_leases = await self._count(
                db, TaskLease, TaskLease.expires_at.is_not(None) & (TaskLease.expires_at <= now)
            )
            dead_letters = await self._count(db, DeadLetter, DeadLetter.status == "open")
            outbox_count, outbox_oldest = await self._count_oldest(
                db, OutboxMessage, OutboxMessage.published_at.is_(None)
            )
            effect_count, effect_oldest = await self._count_oldest(
                db, EffectIntent, EffectIntent.status.in_(["pending", "unknown", "reconciling"])
            )
            reservations = await db.execute(
                select(
                    func.count(ModelReservation.id),
                    func.coalesce(func.sum(ModelReservation.reserved_usd), 0),
                ).where(ModelReservation.status.in_(["pending", "unknown"]))
            )
            reservation_count, reservation_usd = reservations.one()
            artifact_result = await db.execute(
                select(
                    func.count(RunArtifact.id),
                    func.coalesce(func.sum(RunArtifact.size_bytes), 0),
                )
            )
            artifact_count, artifact_bytes = artifact_result.one()
            recent_artifact_bytes = await db.execute(
                select(func.coalesce(func.sum(RunArtifact.size_bytes), 0)).where(
                    RunArtifact.created_at >= now - timedelta(hours=24)
                )
            )
            github_count, github_oldest = await self._count_oldest(
                db,
                GitHubWebhookOutbox,
                GitHubWebhookOutbox.status.in_(["pending", "processing"]),
            )
            cleanup_failures = await self._count(
                db,
                PostIntegrationStep,
                (PostIntegrationStep.kind == "cleanup") & (PostIntegrationStep.status == "failed"),
            )
            available_routes = await db.execute(
                select(func.count(func.distinct(ModelDeployment.id)))
                .select_from(ModelDeployment)
                .join(ProviderAccount, ProviderAccount.id == ModelDeployment.provider_account_id)
                .join(AliasDeployment, AliasDeployment.deployment_id == ModelDeployment.id)
                .join(ModelAlias, ModelAlias.id == AliasDeployment.alias_id)
                .join(PoolAlias, PoolAlias.alias_id == ModelAlias.id)
                .join(ModelPool, ModelPool.id == PoolAlias.pool_id)
                .where(
                    ModelDeployment.enabled.is_(True),
                    ProviderAccount.enabled.is_(True),
                    ModelAlias.enabled.is_(True),
                    or_(ModelDeployment.cooldown_until.is_(None), ModelDeployment.cooldown_until <= now),
                    or_(
                        (ModelPool.kind == "interactive")
                        & (ModelDeployment.active_interactive < ModelDeployment.max_concurrency),
                        (ModelPool.kind == "probe")
                        & (ModelDeployment.active_probe < ModelDeployment.max_concurrency),
                    ),
                )
            )
            available_route_count = int(available_routes.scalar_one())

        return {
            "queue_runs": float(queued_count),
            "queue_oldest_seconds": self._age_seconds(queued_oldest, now),
            "stale_task_leases": float(stale_leases),
            "dead_letters_open": float(dead_letters),
            "outbox_unpublished": float(outbox_count),
            "outbox_oldest_seconds": self._age_seconds(outbox_oldest, now),
            "effects_unreconciled": float(effect_count),
            "effects_oldest_seconds": self._age_seconds(effect_oldest, now),
            "model_reservations_unreconciled": float(reservation_count),
            "model_reservations_unreconciled_usd": float(reservation_usd),
            "artifacts": float(artifact_count),
            "artifact_bytes": float(artifact_bytes),
            "artifact_bytes_created_24h": float(recent_artifact_bytes.scalar_one()),
            "github_outbox_pending": float(github_count),
            "github_outbox_oldest_seconds": self._age_seconds(github_oldest, now),
            "cleanup_failures": float(cleanup_failures),
            "model_route_candidates_available": float(available_route_count),
        }

    @staticmethod
    async def _count(db: AsyncSession, model: Any, criterion: Any) -> int:
        result = await db.execute(select(func.count(model.id)).where(criterion))
        return int(result.scalar_one())

    @staticmethod
    async def _count_oldest(
        db: AsyncSession, model: Any, criterion: Any
    ) -> tuple[int, datetime | None]:
        result = await db.execute(
            select(func.count(model.id), func.min(model.created_at)).where(criterion)
        )
        count, oldest = result.one()
        return int(count), oldest

    @staticmethod
    def _age_seconds(oldest: datetime | None, now: datetime) -> float:
        if oldest is None:
            return 0.0
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return max(0.0, (now - oldest).total_seconds())

    async def render(self) -> str:
        metrics = await self.snapshot()
        lines = [
            "# HELP harnessa_operational_metrics_collection_success Whether durable metrics were collected.",
            "# TYPE harnessa_operational_metrics_collection_success gauge",
            f"harnessa_operational_metrics_collection_success {metrics.pop('collection_success'):.0f}",
        ]
        for name, value in sorted(metrics.items()):
            lines.extend(
                [
                    f"# TYPE harnessa_{name} gauge",
                    f"harnessa_{name} {value:.8f}",
                ]
            )
        return "\n".join(lines) + "\n"


operational_metrics = OperationalMetrics()
