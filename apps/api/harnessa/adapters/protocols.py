"""Typed extension points for optional external runtimes and storage backends."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.contracts import EventEnvelope, ModelRouteRequest, SpecificationRevision


class ModelRouter(Protocol):
    async def complete(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        request: ModelRouteRequest,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    async def stream(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        project_id: UUID,
        request: ModelRouteRequest,
        idempotency_key: str,
    ) -> tuple[UUID, AsyncIterator[bytes]]: ...


class SpecificationAdapter(Protocol):
    async def validate(self, revision: SpecificationRevision) -> Mapping[str, Any]: ...


class AgentSession(Protocol):
    async def initialize(self, capabilities: Sequence[str]) -> Mapping[str, Any]: ...

    async def prompt(self, session_id: str, content: str) -> AsyncIterator[Mapping[str, Any]]: ...

    async def cancel(self, session_id: str) -> None: ...


class UIEventProjector(Protocol):
    def project(self, event: EventEnvelope) -> Sequence[Mapping[str, Any]]: ...


class ArtifactStore(Protocol):
    async def put(self, content: AsyncIterator[bytes], *, media_type: str) -> Mapping[str, Any]: ...

    async def get(self, digest: str) -> AsyncIterator[bytes]: ...


class HarnessEvaluator(Protocol):
    async def evaluate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]: ...
