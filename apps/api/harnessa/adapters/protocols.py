"""Typed extension points for optional external runtimes and storage backends."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.contracts import (
    EventEnvelope,
    ModelRouteRequest,
    RunAnswerRequest,
    RunCancelRequest,
    RunCheckpointWrite,
    RunInterruptRequest,
    RunOrchestrationReceipt,
    RunReconcileRequest,
    RunResumeRequest,
    RunStartRequest,
    SpecificationRevision,
)


class RunOrchestrator(Protocol):
    """Framework-neutral run port; Harnessa remains authoritative for every request."""

    async def start(self, request: RunStartRequest) -> RunOrchestrationReceipt: ...

    async def checkpoint(self, request: RunCheckpointWrite) -> RunOrchestrationReceipt: ...

    async def interrupt(self, request: RunInterruptRequest) -> RunOrchestrationReceipt: ...

    async def answer(self, request: RunAnswerRequest) -> RunOrchestrationReceipt: ...

    async def resume(self, request: RunResumeRequest) -> RunOrchestrationReceipt: ...

    async def cancel(self, request: RunCancelRequest) -> RunOrchestrationReceipt: ...

    async def reconcile(self, request: RunReconcileRequest) -> RunOrchestrationReceipt: ...


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
