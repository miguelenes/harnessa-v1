import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic.json_schema import models_json_schema
from redis.asyncio import Redis
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from harnessa.api.evaluation_routes import (
    EvaluationDryRun,
    EvaluationRunCreate,
    EvaluationRunRead,
)
from harnessa.api.evaluation_routes import (
    router as evaluation_router,
)
from harnessa.api.evidence_routes import router as evidence_router
from harnessa.api.execution_routes import (
    ExecutionAdapterRead,
    ExecutionAdapterWrite,
)
from harnessa.api.execution_routes import (
    router as execution_router,
)
from harnessa.api.github_routes import router as github_router
from harnessa.api.harness_routes import (
    ExperimentCreate,
    ExperimentRead,
    HarnessOverview,
    PromotionRequest,
)
from harnessa.api.harness_routes import router as harness_router
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
from harnessa.observability import operational_metrics, request_metrics
from harnessa.tracing import (
    configure_tracing,
    current_trace_id,
    extract_trace_context,
    shutdown_tracing,
    start_redacted_span,
    tracer_for,
)

configure_tracing("harnessa-api")


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
    shutdown_tracing()


app = FastAPI(title="Harnessa API", version="0.1.0", lifespan=lifespan)
request_logger = logging.getLogger("harnessa.http")

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
    ExperimentCreate,
    ExperimentRead,
    HarnessOverview,
    EvaluationRunCreate,
    EvaluationRunRead,
    EvaluationDryRun,
    ExecutionAdapterRead,
    ExecutionAdapterWrite,
    PromotionRequest,
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
    allow_private_network=True,
    allow_headers=[
        "Content-Type",
        "X-CSRF-Token",
        "X-Bootstrap-Token",
        "Idempotency-Key",
        "If-Match",
        "X-Request-ID",
        "X-Trace-ID",
        "traceparent",
        "tracestate",
    ],
    expose_headers=["X-Request-ID", "X-Trace-ID"],
)


async def _security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    supplied_request_id = request.headers.get("x-request-id", "")
    try:
        request_id = str(UUID(supplied_request_id))
    except (ValueError, AttributeError):
        request_id = str(uuid4())
    supplied_trace_id = request.headers.get("x-trace-id", "")
    trace_id = current_trace_id() or (
        supplied_trace_id
        if re.fullmatch(r"[0-9a-fA-F]{32}", supplied_trace_id)
        else uuid4().hex
    )
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    started = time.perf_counter()
    response: Response | None = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        path = request.url.path
        is_github_webhook = path == "/webhooks/github"
        if (
            path != "/auth/bootstrap"
            and not is_github_webhook
            and (not origin or origin not in origins)
        ):
            response = JSONResponse(
                ErrorResponse(
                    code="origin_denied",
                    message="Origin is not allowed",
                    request_id=request_id,
                ).model_dump(),
                status_code=403,
                headers={"X-Request-ID": request_id},
            )
        if response is None and path not in {"/auth/bootstrap", "/auth/csrf"} and not is_github_webhook:
            cookie = request.cookies.get("harnessa_csrf", "")
            header = request.headers.get("x-csrf-token", "")
            if not cookie or not secrets.compare_digest(cookie, header):
                response = JSONResponse(
                    ErrorResponse(
                        code="csrf_failed",
                        message="CSRF validation failed",
                        request_id=request_id,
                    ).model_dump(),
                    status_code=403,
                    headers={"X-Request-ID": request_id},
                )
    if response is None:
        response = await call_next(request)
    duration = time.perf_counter() - started
    route = getattr(request.scope.get("route"), "path", "/unmatched")
    if route != "/internal/metrics":
        request_metrics.observe(request.method, route, response.status_code, duration)
    request_logger.info(
        json.dumps(
            {
                "event": "http.request",
                "request_id": request_id,
                "trace_id": trace_id,
                "method": request.method,
                "route": route,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 3),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Trace-ID"] = trace_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.middleware("http")
async def trace_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Create a server span before security checks and expose its trace identifier."""
    carrier = dict(request.headers.items())
    context = extract_trace_context(carrier)
    if not trace.get_current_span(context).get_span_context().is_valid:
        legacy_trace_id = request.headers.get("x-trace-id", "")
        if re.fullmatch(r"[0-9a-fA-F]{32}", legacy_trace_id):
            # Preserve the established X-Trace-ID contract while creating a valid
            # W3C remote parent for spans from legacy clients.
            context = extract_trace_context(
                {"traceparent": f"00-{legacy_trace_id.lower()}-{uuid4().hex[:16]}-01"}
            )
    with start_redacted_span(
        tracer_for(__name__),
        f"HTTP {request.method}",
        context=context,
        kind=SpanKind.SERVER,
    ) as span:
        try:
            response = await _security_headers(request, call_next)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        route = getattr(request.scope.get("route"), "path", "/unmatched")
        span.set_attribute("http.route", route)
        span.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR))
        return response


@app.get("/internal/metrics", include_in_schema=False, response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    configured_token = settings.metrics_token
    authorization = request.headers.get("authorization", "")
    presented_token = authorization.removeprefix("Bearer ")
    expected_token = configured_token.get_secret_value() if configured_token is not None else ""
    if (
        not expected_token
        or not authorization.startswith("Bearer ")
        or not secrets.compare_digest(presented_token, expected_token)
    ):
        raise HTTPException(status_code=404, detail="Not found")
    durable_metrics = await operational_metrics.render()
    return PlainTextResponse(
        request_metrics.render() + durable_metrics,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


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
app.include_router(harness_router)
app.include_router(evaluation_router)
app.include_router(execution_router)
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
