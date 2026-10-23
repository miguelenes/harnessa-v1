"""Provider port for publishing immutable review bundles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harnessa.db.review import ReviewBundle


@dataclass(frozen=True)
class PublicationReceipt:
    provider: str
    reference: str
    bundle_digest: str


class ReviewPublicationPort(Protocol):
    def publish(self, bundle: ReviewBundle) -> PublicationReceipt: ...


class LocalReviewPublicationPort:
    """Expose a local immutable bundle reference; remote publication can be added later."""

    def publish(self, bundle: ReviewBundle) -> PublicationReceipt:
        return PublicationReceipt(
            provider="local",
            reference=f"/tasks/{bundle.task_id}/review-bundle?digest={bundle.bundle_digest}",
            bundle_digest=bundle.bundle_digest,
        )
