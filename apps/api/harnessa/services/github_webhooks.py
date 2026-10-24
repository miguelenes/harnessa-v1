"""Transactional webhook inbox/outbox delivery with leases and bounded retries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from harnessa.config import settings
from harnessa.db.github import GitHubWebhookDelivery, GitHubWebhookOutbox
from harnessa.db.session import SessionLocal
from harnessa.tracing import start_redacted_span, trace_context_from_parent, tracer_for
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DeliveryState = Literal["processed", "ignored"]


@dataclass(frozen=True)
class DeliveryOutcome:
    state: DeliveryState
    result: dict[str, Any]


@dataclass(frozen=True)
class DeliveryEnvelope:
    id: UUID
    organization_id: UUID
    app_configuration_id: UUID
    event_name: str
    action: str | None
    installation_number: int | None
    github_repository_id: int | None
    payload: dict[str, Any]
    delivery_id: str
    traceparent: str | None = None


class DeliveryProcessor(Protocol):
    async def process(self, delivery: DeliveryEnvelope) -> DeliveryOutcome: ...


async def process_github_webhook_outbox(
    processor: DeliveryProcessor,
    *,
    owner_id: str,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    limit: int = 20,
    max_attempts: int | None = None,
) -> int:
    """Claim durable intents, invoke an idempotent handler, then record its outcome."""
    if limit < 1 or limit > 100:
        raise ValueError("Webhook batch limit must be between 1 and 100")
    attempt_limit = max_attempts or settings.github_webhook_max_attempts
    now = datetime.now(UTC)
    claimed: list[tuple[UUID, UUID, DeliveryEnvelope, int]] = []
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(GitHubWebhookOutbox, GitHubWebhookDelivery)
                .join(
                    GitHubWebhookDelivery,
                    GitHubWebhookDelivery.id == GitHubWebhookOutbox.delivery_id,
                )
                .where(
                    or_(
                        and_(
                            GitHubWebhookOutbox.status == "pending",
                            or_(
                                GitHubWebhookOutbox.next_attempt_at.is_(None),
                                GitHubWebhookOutbox.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            GitHubWebhookOutbox.status == "processing",
                            GitHubWebhookOutbox.lease_until <= now,
                        ),
                    ),
                    GitHubWebhookDelivery.status.in_(["pending", "processing"]),
                )
                .order_by(GitHubWebhookOutbox.created_at)
                .with_for_update(skip_locked=True, of=GitHubWebhookOutbox)
                .limit(limit)
            )
        ).all()
        for outbox, delivery in rows:
            outbox.status = "processing"
            outbox.attempts += 1
            outbox.lease_owner = owner_id
            outbox.lease_until = now + timedelta(seconds=60)
            delivery.status = "processing"
            delivery.attempts += 1
            delivery.last_error = None
            claimed.append(
                (
                    outbox.id,
                    delivery.id,
                    DeliveryEnvelope(
                        id=delivery.id,
                        organization_id=delivery.organization_id,
                        app_configuration_id=delivery.app_configuration_id,
                        event_name=delivery.event_name,
                        action=delivery.action,
                        installation_number=delivery.installation_number,
                        github_repository_id=delivery.github_repository_id,
                        payload=delivery.payload,
                        delivery_id=delivery.delivery_id,
                        traceparent=delivery.traceparent,
                    ),
                    outbox.attempts,
                )
            )
        await db.commit()

    for outbox_id, delivery_id, envelope, attempts in claimed:
        with start_redacted_span(
            tracer_for(__name__),
            "harnessa.github.webhook.process",
            context=trace_context_from_parent(envelope.traceparent),
            kind=SpanKind.CONSUMER,
            attributes={
                "harnessa.github.delivery_id": envelope.delivery_id,
                "harnessa.github.event_name": envelope.event_name,
                "harnessa.github.action": envelope.action or "unknown",
            },
        ) as span:
            try:
                outcome = await processor.process(envelope)
                state: DeliveryState = outcome.state
                result = outcome.result
                safe_error = None
            except Exception as exc:  # noqa: BLE001 - adapter and content errors share one retry boundary.
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", type(exc).__name__)
                state = "ignored"
                result = {}
                safe_error = f"{type(exc).__name__}: webhook handler failed"[:1000]
        async with session_factory() as db:
            outbox = await db.get(GitHubWebhookOutbox, outbox_id)
            delivery = await db.get(GitHubWebhookDelivery, delivery_id)
            if (
                outbox is None
                or delivery is None
                or outbox.status != "processing"
                or outbox.lease_owner != owner_id
            ):
                continue
            outbox.lease_owner = None
            outbox.lease_until = None
            if safe_error is None:
                outbox.status = "completed"
                outbox.last_error = None
                delivery.status = state
                delivery.result = result
                delivery.last_error = None
                delivery.processed_at = datetime.now(UTC)
            elif attempts >= attempt_limit:
                outbox.status = "completed"
                outbox.last_error = safe_error
                delivery.status = "failed"
                delivery.last_error = safe_error
                delivery.processed_at = datetime.now(UTC)
            else:
                delay = min(300, 2 ** min(attempts, 8))
                outbox.status = "pending"
                outbox.last_error = safe_error
                outbox.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                delivery.status = "pending"
                delivery.last_error = safe_error
                delivery.next_attempt_at = outbox.next_attempt_at
            await db.commit()
    return len(claimed)
