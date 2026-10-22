"""Redis Streams transport over the PostgreSQL event/outbox authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from harnessa.config import settings
from harnessa.db.models import Task
from harnessa.db.orchestration import ConsumerInbox, DomainEvent, OutboxMessage
from harnessa.db.session import SessionLocal
from redis.asyncio import Redis
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

STREAM_NAME = "harnessa:domain-events:v1"
CONSUMER_GROUP = "harnessa-domain-consumers-v1"


@dataclass(frozen=True)
class OutboxItem:
    id: UUID
    event_id: UUID
    event_type: str
    project_id: UUID
    project_sequence: int
    payload: dict[str, Any]


class OutboxPublisher:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        *,
        owner_id: str,
    ) -> None:
        self.redis = redis
        self.sessions = session_factory
        self.owner_id = owner_id

    async def claim_batch(self, limit: int = 100) -> list[OutboxItem]:
        now = datetime.now(UTC)
        async with self.sessions() as db:
            rows = (
                await db.execute(
                    select(OutboxMessage, DomainEvent)
                    .join(DomainEvent, DomainEvent.id == OutboxMessage.event_id)
                    .where(
                        OutboxMessage.published_at.is_(None),
                        or_(OutboxMessage.next_attempt_at.is_(None), OutboxMessage.next_attempt_at <= now),
                        or_(OutboxMessage.lease_until.is_(None), OutboxMessage.lease_until <= now),
                    )
                    .order_by(OutboxMessage.created_at)
                    .with_for_update(skip_locked=True, of=OutboxMessage)
                    .limit(limit)
                )
            ).all()
            items: list[OutboxItem] = []
            for outbox, event in rows:
                outbox.lease_owner = self.owner_id
                outbox.lease_until = now + timedelta(seconds=30)
                outbox.attempts += 1
                items.append(
                    OutboxItem(
                        outbox.id,
                        event.id,
                        event.event_type,
                        event.project_id,
                        event.project_sequence,
                        event.payload,
                    )
                )
            await db.commit()
            return items

    async def publish_once(self, limit: int = 100) -> int:
        items = await self.claim_batch(limit)
        published = 0
        for item in items:
            fields = {
                "event_id": str(item.event_id),
                "event_type": item.event_type,
                "project_id": str(item.project_id),
                "project_sequence": str(item.project_sequence),
                "payload": json.dumps(item.payload, sort_keys=True, separators=(",", ":")),
            }
            try:
                await self.redis.xadd(
                    STREAM_NAME,
                    cast(Any, fields),
                    maxlen=settings.redis_stream_maxlen,
                    approximate=True,
                )
            except Exception as exc:  # noqa: BLE001 - failed publishes must remain retryable.
                async with self.sessions() as db:
                    row = await db.get(OutboxMessage, item.id)
                    if row and row.lease_owner == self.owner_id and row.published_at is None:
                        row.last_error = str(exc)[:2000]
                        row.lease_owner = None
                        row.lease_until = None
                        row.next_attempt_at = datetime.now(UTC) + timedelta(
                            seconds=min(300, 2 ** min(row.attempts, 8))
                        )
                        await db.commit()
                continue
            async with self.sessions() as db:
                row = await db.get(OutboxMessage, item.id)
                if row and row.lease_owner == self.owner_id and row.published_at is None:
                    row.published_at = datetime.now(UTC)
                    row.lease_owner = None
                    row.lease_until = None
                    row.last_error = None
                    await db.commit()
                    published += 1
        return published


class StreamConsumer:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        *,
        consumer_name: str,
        min_idle_ms: int = 30_000,
    ) -> None:
        self.redis = redis
        self.sessions = session_factory
        self.consumer_name = consumer_name
        self.min_idle_ms = min_idle_ms

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def process_once(
        self, *, count: int = 20, block_ms: int = 1000, crash_after_commit: bool = False
    ) -> int:
        await self.ensure_group()
        claimed = await self.redis.xautoclaim(
            STREAM_NAME,
            CONSUMER_GROUP,
            self.consumer_name,
            min_idle_time=self.min_idle_ms,
            start_id="0-0",
            count=count,
        )
        messages = _normalize_stream_messages(_nested_item(claimed, 1))
        if not messages:
            response = await self.redis.xreadgroup(
                CONSUMER_GROUP,
                self.consumer_name,
                {STREAM_NAME: ">"},
                count=count,
                block=block_ms,
            )
            messages = _normalize_stream_messages(_nested_item(_nested_item(response, 0), 1))

        handled = 0
        for message_id, fields in messages:
            raw_id = fields.get(b"event_id") or fields.get("event_id")
            if raw_id is None:
                continue
            event_id = UUID(raw_id.decode() if isinstance(raw_id, bytes) else raw_id)
            async with self.sessions() as db:
                event = await db.get(DomainEvent, event_id)
                if event is None:
                    # Retention never removes events without a durable inbox receipt.
                    continue
                result = await db.execute(
                    insert(ConsumerInbox)
                    .values(
                        id=uuid4(),
                        consumer=CONSUMER_GROUP,
                        event_id=event_id,
                        result={"event_type": event.event_type, "durably_handled": True},
                    )
                    .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
                    .returning(ConsumerInbox.id)
                )
                newly_handled = result.scalar_one_or_none() is not None
                if newly_handled:
                    # Scheduler dispatch is a DB query, so this inbox receipt is sufficient:
                    # the event cannot itself launch an agent or become dispatch authority.
                    pass
                await db.commit()
            if crash_after_commit:
                raise RuntimeError("Injected crash after durable inbox commit, before XACK")
            await self.redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
            handled += 1 if newly_handled else 0
        return handled


def _nested_item(value: object, index: int) -> object:
    if isinstance(value, (list, tuple)) and len(value) > index:
        return value[index]
    return None


def _normalize_stream_messages(value: object) -> list[tuple[str, dict[Any, Any]]]:
    if not isinstance(value, (list, tuple)):
        return []
    messages: list[tuple[str, dict[Any, Any]]] = []
    for row in value:
        raw_id = _nested_item(row, 0)
        fields = _nested_item(row, 1)
        if isinstance(raw_id, bytes):
            message_id = raw_id.decode("utf-8", errors="replace")
        elif isinstance(raw_id, str):
            message_id = raw_id
        else:
            continue
        if isinstance(fields, dict):
            messages.append((message_id, fields))
    return messages


async def rebuild_dispatch_stream(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    limit: int = 10_000,
) -> int:
    """Rebuild the Redis projection from still-dispatchable retained DB state."""
    from harnessa.contracts import RunStatus, TaskStage

    await redis.delete(STREAM_NAME)
    async with session_factory() as db:
        latest = (
            await db.execute(
                select(DomainEvent)
                .join(Task, Task.id == DomainEvent.task_id)
                .where(
                    DomainEvent.event_type == "task.ready",
                    DomainEvent.retention_until > datetime.now(UTC),
                    Task.stage == TaskStage.READY.value,
                    Task.run_status == RunStatus.QUEUED.value,
                    Task.cancel_requested_at.is_(None),
                )
                .distinct(DomainEvent.task_id)
                .order_by(DomainEvent.task_id, DomainEvent.project_sequence.desc())
                .limit(limit)
            )
        ).scalars().all()
    for event in latest:
        await redis.xadd(
            STREAM_NAME,
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "project_id": str(event.project_id),
                "project_sequence": str(event.project_sequence),
                "payload": json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            },
            maxlen=settings.redis_stream_maxlen,
            approximate=True,
        )
    await StreamConsumer(redis, session_factory, consumer_name="rebuild").ensure_group()
    return len(latest)


async def prune_retained_events(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> int:
    now = datetime.now(UTC)
    async with session_factory() as db:
        result = await db.execute(
            delete(DomainEvent)
            .where(
                DomainEvent.retention_until <= now,
                DomainEvent.id.in_(
                    select(OutboxMessage.event_id).where(OutboxMessage.published_at.is_not(None))
                ),
                DomainEvent.id.in_(
                    select(ConsumerInbox.event_id).where(ConsumerInbox.consumer == CONSUMER_GROUP)
                ),
            )
        )
        await db.commit()
        return cast(Any, result).rowcount or 0
