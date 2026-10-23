"""Bounded reconciliation of durable gateway usage import receipts."""

from datetime import UTC, datetime, timedelta

from harnessa.db.model_gateway import GatewayUsageImport, ModelRequestLedger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_USAGE_IMPORT_ATTEMPTS = 12


async def reconcile_gateway_usage_receipt(
    db: AsyncSession, receipt: GatewayUsageImport, *, now: datetime | None = None
) -> GatewayUsageImport:
    """Match one locked receipt and append at most one ledger correction event."""
    observed_now = now or datetime.now(UTC)
    existing_event = (
        await db.execute(
            select(ModelRequestLedger).where(
                ModelRequestLedger.project_id == receipt.project_id,
                ModelRequestLedger.gateway_usage_source_observation_id
                == receipt.source_observation_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        receipt.status = "matched"
        receipt.ledger_event_id = existing_event.id
        receipt.next_retry_at = None
        return receipt

    candidates = list(
        (
            await db.execute(
                select(ModelRequestLedger).where(
                    ModelRequestLedger.project_id == receipt.project_id,
                    ModelRequestLedger.request_identifier == receipt.request_identifier,
                    ModelRequestLedger.event_type == "attempt_succeeded",
                )
            )
        )
        .scalars()
        .all()
    )
    receipt.attempts += 1
    receipt.next_retry_at = None
    if len(candidates) == 1:
        matched = candidates[0]
        event = ModelRequestLedger(
            request_id=matched.request_id,
            attempt=matched.attempt,
            event_type="usage_reconciled",
            organization_id=matched.organization_id,
            project_id=matched.project_id,
            task_id=matched.task_id,
            run_id=matched.run_id,
            account_id=matched.account_id,
            deployment_id=matched.deployment_id,
            pool_name=matched.pool_name,
            model_alias=matched.model_alias,
            actual_model=matched.actual_model,
            request_identifier=matched.request_identifier,
            route_decision=matched.route_decision,
            price_revision=matched.price_revision,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            usage_known=receipt.input_tokens is not None or receipt.output_tokens is not None,
            billed_usd=receipt.gateway_billed_usd,
            provider_reported_usd=receipt.provider_reported_usd,
            http_status=matched.http_status,
            details={
                "source_observation_id": receipt.source_observation_id,
                "submitted_by": str(receipt.submitted_by),
            },
            gateway_usage_source_observation_id=receipt.source_observation_id,
        )
        db.add(event)
        await db.flush()
        receipt.status = "matched"
        receipt.ledger_event_id = event.id
    elif len(candidates) > 1:
        receipt.status = "ambiguous"
    elif receipt.attempts >= MAX_USAGE_IMPORT_ATTEMPTS:
        receipt.status = "failed"
    else:
        receipt.status = "retryable"
        receipt.next_retry_at = observed_now + timedelta(
            seconds=min(3600, 30 * (2 ** min(receipt.attempts - 1, 7)))
        )
    return receipt


async def reconcile_due_gateway_usage_imports(
    session_factory: async_sessionmaker[AsyncSession], *, limit: int = 100
) -> int:
    """Claim due receipts with SKIP LOCKED and reconcile no more than ``limit`` rows."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    async with session_factory() as db:
        now = datetime.now(UTC)
        receipts = list(
            (
                await db.execute(
                    select(GatewayUsageImport)
                    .where(
                        GatewayUsageImport.status == "retryable",
                        GatewayUsageImport.next_retry_at <= now,
                    )
                    .order_by(GatewayUsageImport.next_retry_at, GatewayUsageImport.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for receipt in receipts:
            await reconcile_gateway_usage_receipt(db, receipt, now=now)
        await db.commit()
        return len(receipts)
