"""Exact-head approval checks shared by local review and GitHub publication."""

from __future__ import annotations

from harnessa.db.review import ReviewBundle, ReviewDecision
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def has_current_review_approval(db: AsyncSession, bundle: ReviewBundle) -> bool:
    """Return whether an approval is bound to this exact immutable bundle and head."""
    decision_id = await db.scalar(
        select(ReviewDecision.id)
        .where(
            ReviewDecision.bundle_id == bundle.id,
            ReviewDecision.decision == "approve",
            ReviewDecision.bundle_digest == bundle.bundle_digest,
            ReviewDecision.head_sha == bundle.head_sha,
        )
        .limit(1)
    )
    return decision_id is not None
