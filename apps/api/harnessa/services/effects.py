"""Stable-key external-effect reconciliation without assuming exactly-once delivery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from harnessa.db.orchestration import EffectIntent
from harnessa.db.session import SessionLocal
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ReconcileOutcome = Literal["applied", "not_applied", "unknown"]


class EffectReconciler(Protocol):
    async def reconcile(
        self, *, kind: str, stable_key: str, payload: Mapping[str, object]
    ) -> ReconcileOutcome: ...


async def reconcile_effect_intents(
    adapter: EffectReconciler,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    limit: int = 50,
) -> int:
    """Mark expired reconciling intents unknown before an adapter checks remote state."""
    now = datetime.now(UTC)
    claimed: list[tuple[UUID, str, str, dict[str, Any]]] = []
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(EffectIntent)
                .where(
                    or_(
                        EffectIntent.status.in_(["pending", "unknown"])
                        & or_(EffectIntent.next_attempt_at.is_(None), EffectIntent.next_attempt_at <= now),
                        (EffectIntent.status == "reconciling")
                        & (EffectIntent.updated_at <= now - timedelta(seconds=60)),
                    ),
                )
                .order_by(EffectIntent.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).scalars().all()
        for row in rows:
            row.status = "reconciling"
            row.attempts += 1
            claimed.append((row.id, row.kind, row.stable_key, row.payload))
        await db.commit()

    for intent_id, kind, stable_key, payload in claimed:
        error: str | None = None
        try:
            outcome = await adapter.reconcile(kind=kind, stable_key=stable_key, payload=payload)
        except Exception as exc:  # noqa: BLE001 - arbitrary adapter failures have unknown effects.
            outcome = "unknown"
            error = str(exc)[:2000]
        async with session_factory() as db:
            intent_row = await db.get(EffectIntent, intent_id)
            if intent_row is None or intent_row.status != "reconciling":
                continue
            intent_row.status = "completed" if outcome == "applied" else (
                "pending" if outcome == "not_applied" else "unknown"
            )
            intent_row.last_error = error
            intent_row.reconciled_at = datetime.now(UTC)
            intent_row.next_attempt_at = (
                None if outcome == "applied" else
                datetime.now(UTC)
                + timedelta(seconds=min(300, 2 ** min(intent_row.attempts, 8)))
            )
            await db.commit()
    return len(claimed)
