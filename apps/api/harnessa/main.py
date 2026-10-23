import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic.json_schema import models_json_schema
from redis.asyncio import Redis
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from harnessa.api.evidence_routes import router as evidence_router
from harnessa.api.github_routes import router as github_router
from harnessa.api.model_routes import router as model_router
from harnessa.api.retrieval_routes import router as retrieval_router
from harnessa.api.review_routes import router as review_router
from harnessa.api.routes import router
from harnessa.api.specification_routes import router as specification_router
from harnessa.api.task_routes import router as task_router
from harnessa.config import settings
from harnessa.contracts import (
    ArtifactMetadata,
    AuthorizationContext,
    CoreEvidenceEnvelope,
    DeadLetterRead,
    DispatchRebuildResult,
    ErrorResponse,
    EventEnvelope,
    EventReplayItem,
    EventReplayPage,
    EvidenceIntegerValue,
    EvidenceIntegrityRead,
    EvidencePage,
    EvidenceRecordRead,
    EvidenceTextValue,
    EvidenceTimestampValue,
    EvidenceUuidValue,
    GatewayUsageImportBatch,
    GatewayUsageImportResult,
    GitHubActorMappingCreate,
    GitHubActorMappingRead,
    GitHubAppConfigurationCreate,
    GitHubAppConfigurationRead,
    GitHubConnectionStatusRead,
    GitHubDeliveryRepairRequest,
    GitHubEffectQueuedRead,
    GitHubInstallationCreate,
    GitHubInstallationRead,
    GitHubPublishRequest,
    GitHubRepositoryLinkRead,
    GitHubRepositoryOptIn,
    GitHubWebhookReceipt,
    Harness2ArtifactEvidence,
    Harness2ImportFixture,
    Identity,
    MCPGrantCreate,
    MCPGrantRead,
    MCPServerStatusRead,
    MCPToolInvokeRequest,
    MCPToolInvokeResponse,
    ModelAliasRead,
    ModelBudgetPolicyRead,
    ModelDeploymentRead,
    ModelDoctorRead,
    ModelGatewayConfiguration,
    ModelInvocationRead,
    ModelLiveCheckRead,
    ModelPoolRead,
    ModelProviderAccountRead,
    ModelRequestLedgerRead,
    ModelRouteRequest,
    ModelUsageRead,
    RecoveryRequest,
    RepositoryCitation,
    RepositoryIndexSyncRequest,
    RepositoryIndexSyncResponse,
    RepositorySearchRequest,
    RepositorySearchResponse,
    RetrievalContextManifestRead,
    RetrievalManifestItemRead,
    RetrievalSourceStatusRead,
    RetrievalStatusRead,
    RunAnswerRequest,
    RunCancelRequest,
    RunCheckpointReference,
    RunCheckpointWrite,
    RunInterruptRequest,
    RunOrchestrationIdentity,
    RunOrchestrationReceipt,
    RunRead,
    RunReconcileRequest,
    RunResumeRequest,
    RunStartRequest,
    SpecificationAnswerCreate,
    SpecificationAnswerRead,
    SpecificationAnswerResult,
    SpecificationArchitectRead,
    SpecificationArchitectRequest,
    SpecificationArtifactRead,
    SpecificationArtifactUpdate,
    SpecificationExplorerEntry,
    SpecificationExplorerRead,
    SpecificationInterviewCreate,
    SpecificationInterviewRead,
    SpecificationQuestionRead,
    SpecificationReadinessRead,
    SpecificationReadinessSignoff,
    SpecificationRevision,
    TaskCapabilitiesRead,
    TaskCommandRequest,
    TaskCreate,
    TaskRead,
    TaskState,
    TaskTransitionRead,
    TaskTransitionRequest,
    VerificationManifestApprovalRequest,
    VerificationManifestRevisionRead,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.app_env == "production" and (
        len(settings.secret_key) < 32 or not settings.bootstrap_token or not settings.cookie_secure
    ):
        raise RuntimeError(
            "Production requires SECRET_KEY, BOOTSTRAP_TOKEN, and COOKIE_SECURE=true"
        )
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="Harnessa API", version="0.1.0", lifespan=lifespan)

# These contracts are canonical even before their milestone-specific endpoints exist.
# Keep them server-owned and publish them in OpenAPI for generated client types.
CONTRACT_MODELS = (
    EventEnvelope,
    ErrorResponse,
    Identity,
    AuthorizationContext,
    TaskState,
    TaskCreate,
    TaskRead,
    TaskCapabilitiesRead,
    TaskTransitionRequest,
    TaskCommandRequest,
    RunRead,
    EventReplayPage,
    EventReplayItem,
    DeadLetterRead,
    RecoveryRequest,
    TaskTransitionRead,
    DispatchRebuildResult,
    SpecificationRevision,
    ArtifactMetadata,
    ModelRouteRequest,
    ModelProviderAccountRead,
    ModelDeploymentRead,
    ModelAliasRead,
    ModelPoolRead,
    ModelBudgetPolicyRead,
    ModelUsageRead,
    ModelDoctorRead,
    ModelLiveCheckRead,
    ModelGatewayConfiguration,
    ModelInvocationRead,
    ModelRequestLedgerRead,
    EvidenceTextValue,
    EvidenceUuidValue,
    EvidenceIntegerValue,
    EvidenceTimestampValue,
    CoreEvidenceEnvelope,
    EvidencePage,
    EvidenceRecordRead,
    EvidenceIntegrityRead,
    GatewayUsageImportBatch,
    GatewayUsageImportResult,
    Harness2ArtifactEvidence,
    Harness2ImportFixture,
    VerificationManifestApprovalRequest,
    VerificationManifestRevisionRead,
    RunOrchestrationIdentity,
    RunCheckpointReference,
    RunStartRequest,
    RunCheckpointWrite,
    RunInterruptRequest,
    RunAnswerRequest,
    RunResumeRequest,
    RunCancelRequest,
    RunReconcileRequest,
    RunOrchestrationReceipt,
    RepositoryCitation,
    RepositorySearchRequest,
    RepositorySearchResponse,
    RetrievalContextManifestRead,
    RetrievalManifestItemRead,
    RetrievalSourceStatusRead,
    RetrievalStatusRead,
    MCPServerStatusRead,
    MCPGrantCreate,
    MCPGrantRead,
    MCPToolInvokeRequest,
    MCPToolInvokeResponse,
    RepositoryIndexSyncRequest,
    RepositoryIndexSyncResponse,
    SpecificationInterviewCreate,
    SpecificationInterviewRead,
    SpecificationQuestionRead,
    SpecificationAnswerCreate,
    SpecificationAnswerRead,
    SpecificationAnswerResult,
    SpecificationArtifactUpdate,
    SpecificationArtifactRead,
    SpecificationReadinessRead,
    SpecificationReadinessSignoff,
    SpecificationArchitectRequest,
    SpecificationArchitectRead,
    SpecificationExplorerEntry,
    SpecificationExplorerRead,
    GitHubAppConfigurationCreate,
    GitHubAppConfigurationRead,
    GitHubInstallationCreate,
    GitHubInstallationRead,
    GitHubRepositoryOptIn,
    GitHubRepositoryLinkRead,
    GitHubActorMappingCreate,
    GitHubActorMappingRead,
    GitHubConnectionStatusRead,
    GitHubDeliveryRepairRequest,
    GitHubPublishRequest,
    GitHubEffectQueuedRead,
    GitHubWebhookReceipt,
)


def openapi_with_canonical_contracts() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    _, definitions = models_json_schema(
        [(model, "validation") for model in CONTRACT_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(definitions.pop("$defs", {}))
    schemas.update(definitions)
    app.openapi_schema = schema
    return schema


app.openapi = openapi_with_canonical_contracts  # type: ignore[method-assign]
origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Content-Type",
        "X-CSRF-Token",
        "X-Bootstrap-Token",
        "Idempotency-Key",
        "If-Match",
    ],
)


@app.middleware("http")
async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id = str(uuid4())
    request.state.request_id = request_id
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        path = request.url.path
        is_github_webhook = path == "/webhooks/github"
        if (
            path != "/auth/bootstrap"
            and not is_github_webhook
            and (not origin or origin not in origins)
        ):
            return JSONResponse(
                ErrorResponse(
                    code="origin_denied",
                    message="Origin is not allowed",
                    request_id=request_id,
                ).model_dump(),
                status_code=403,
                headers={"X-Request-ID": request_id},
            )
        if path not in {"/auth/bootstrap", "/auth/csrf"} and not is_github_webhook:
            cookie = request.cookies.get("harnessa_csrf", "")
            header = request.headers.get("x-csrf-token", "")
            if not cookie or not secrets.compare_digest(cookie, header):
                return JSONResponse(
                    ErrorResponse(
                        code="csrf_failed",
                        message="CSRF validation failed",
                        request_id=request_id,
                    ).model_dump(),
                    status_code=403,
                    headers={"X-Request-ID": request_id},
                )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    status_codes = {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
    }
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        error = ErrorResponse(
            code=str(detail["code"]),
            message=str(detail.get("reason", detail.get("message", detail["code"]))),
            request_id=getattr(request.state, "request_id", None),
            details={
                key: value
                for key, value in detail.items()
                if key not in {"code", "reason", "message"}
            },
        )
    else:
        error = ErrorResponse(
            code=status_codes.get(exc.status_code, "http_error"),
            message=str(detail),
            request_id=getattr(request.state, "request_id", None),
        )
    return JSONResponse(
        error.model_dump(),
        status_code=exc.status_code,
        headers={**(exc.headers or {}), "X-Request-ID": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        ErrorResponse(
            code="validation_error",
            message="Request validation failed",
            request_id=request_id,
            details={"errors": errors},
        ).model_dump(),
        status_code=422,
        headers={"X-Request-ID": request_id or ""},
    )


app.include_router(router)
app.include_router(task_router)
app.include_router(model_router)
app.include_router(specification_router)
app.include_router(retrieval_router)
app.include_router(review_router)
app.include_router(github_router)
app.include_router(evidence_router)


@app.get("/health/live", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", response_model=None, tags=["health"])
async def ready(request: Request) -> JSONResponse | dict[str, str]:
    from sqlalchemy import text

    from harnessa.db.session import engine

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await request.app.state.redis.ping()
        if settings.model_gateway_url:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(
                    f"{settings.model_gateway_url.rstrip('/')}/health/readiness"
                )
                response.raise_for_status()
    except Exception:  # noqa: BLE001 - readiness maps any dependency failure to 503.
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}
