"""Authenticated project retrieval status, sync preview, and scoped MCP operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from harnessa.adapters.mcp_http import MCPRemoteError, StreamableHTTPMCPClient
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.contracts import (
    MCPGrantCreate,
    MCPGrantRead,
    MCPServerStatusRead,
    MCPToolInvokeRequest,
    MCPToolInvokeResponse,
    RepositoryIndexSyncRequest,
    RepositoryIndexSyncResponse,
    RetrievalSourceStatusRead,
    RetrievalStatusRead,
)
from harnessa.db.models import Project, Repository, User
from harnessa.db.retrieval import MCPProjectGrant, MCPToolAudit, RetrievalSync
from harnessa.db.session import get_db
from harnessa.services.authorization import require_membership
from harnessa.services.retrieval_governance import (
    MAX_TOOL_OUTPUT_BYTES,
    MAX_TOOL_TIMEOUT_SECONDS,
    SUPPORTED_MCP_PROTOCOLS,
    MCPGrant,
    MCPTool,
    invoke_mcp_tool,
)
from harnessa.services.retrieval_index import sync_repository_index
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["retrieval and MCP"])


def _configured_servers() -> dict[str, str]:
    try:
        value = json.loads(settings.mcp_servers_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "MCP server registry configuration is invalid") from exc
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise HTTPException(503, "MCP server registry must map IDs to endpoint strings")
    return value


def _configured_read_only_tools() -> dict[str, set[str]]:
    try:
        value = json.loads(settings.mcp_read_only_tools_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "MCP read-only tool policy is invalid") from exc
    if not isinstance(value, dict) or any(
        not isinstance(server_id, str)
        or not isinstance(names, list)
        or any(not isinstance(name, str) for name in names)
        for server_id, names in value.items()
    ):
        raise HTTPException(503, "MCP read-only policy must map server IDs to tool name lists")
    return {server_id: set(names) for server_id, names in value.items()}


def _configured_secret_references() -> dict[str, set[str]]:
    try:
        value = json.loads(settings.mcp_secret_references_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, "MCP secret-reference policy is invalid") from exc
    if not isinstance(value, dict) or any(
        not isinstance(server_id, str)
        or not isinstance(names, list)
        or any(not isinstance(name, str) or not name for name in names)
        for server_id, names in value.items()
    ):
        raise HTTPException(503, "MCP secret-reference policy must map server IDs to environment-name lists")
    return {server_id: set(names) for server_id, names in value.items()}


def _grant_read(grant: MCPProjectGrant) -> MCPGrantRead:
    return MCPGrantRead(
        id=grant.id,
        server_id=grant.server_id,
        allowed_tools=grant.allowed_tools,
        read_only_tools=grant.read_only_tools,
        protocol_version=grant.protocol_version,
        has_secret_reference=bool(grant.secret_reference),
        enabled=grant.enabled,
        revoked_at=grant.revoked_at,
    )


@router.get("/projects/{project_id}/retrieval/status", response_model=RetrievalStatusRead)
async def retrieval_status(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> RetrievalStatusRead:
    await require_membership(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    repositories = list(
        await db.scalars(select(Repository).where(Repository.project_id == project_id))
    )
    source_reads: list[RetrievalSourceStatusRead] = []
    for repository in repositories:
        sync = await db.scalar(
            select(RetrievalSync)
            .where(RetrievalSync.repository_id == repository.id)
            .order_by(RetrievalSync.started_at.desc())
            .limit(1)
        )
        latest_successful_sync = await db.scalar(
            select(RetrievalSync)
            .where(
                RetrievalSync.repository_id == repository.id,
                RetrievalSync.state == "succeeded",
            )
            .order_by(RetrievalSync.finished_at.desc())
            .limit(1)
        )
        latest_successful_sync = await db.scalar(
            select(RetrievalSync)
            .where(
                RetrievalSync.repository_id == repository.id,
                RetrievalSync.state == "succeeded",
            )
            .order_by(RetrievalSync.finished_at.desc())
            .limit(1)
        )
        coverage_sync = await db.scalar(
            select(RetrievalSync)
            .where(
                RetrievalSync.repository_id == repository.id,
                RetrievalSync.state == "succeeded",
                RetrievalSync.incremental.is_(False),
                RetrievalSync.snapshot_complete.is_(True),
                RetrievalSync.considered_count > 0,
            )
            .order_by(RetrievalSync.finished_at.desc())
            .limit(1)
        )
        failed_syncs = await db.scalar(
            select(func.count(RetrievalSync.id)).where(
                RetrievalSync.repository_id == repository.id,
                RetrievalSync.state == "failed",
            )
        )
        coverage = 0.0
        if coverage_sync is not None:
            coverage = min(1.0, float(coverage_sync.indexed_count) / coverage_sync.considered_count)
        if sync is None:
            freshness = "not_indexed"
        elif sync.state == "failed":
            freshness = "stale_after_failed_sync"
        elif coverage_sync is None:
            freshness = "full_snapshot_required"
        elif latest_successful_sync and latest_successful_sync.revision != coverage_sync.revision:
            freshness = "incremental_since_coverage_snapshot"
        else:
            freshness = "last_sync_only"
        source_reads.append(
            RetrievalSourceStatusRead(
                repository_id=repository.id,
                source_uri=f"repo://{repository.id}",
                indexed_revision=(latest_successful_sync.revision if latest_successful_sync else None),
                coverage_revision=(coverage_sync.revision if coverage_sync else None),
                freshness=freshness,
                coverage=coverage,
                last_sync_at=sync.finished_at if sync else None,
                failed_syncs=int(failed_syncs or 0),
                permission_scope="current_project_repository_membership",
                omitted_reasons=sorted((sync.omitted_counts if sync else {}).keys()),
            )
        )
    configured = _configured_servers()
    grants = list(
        await db.scalars(
            select(MCPProjectGrant).where(
                MCPProjectGrant.project_id == project_id,
                MCPProjectGrant.enabled.is_(True),
                MCPProjectGrant.revoked_at.is_(None),
            )
        )
    )
    grants_by_server = {grant.server_id: grant for grant in grants}
    server_ids = sorted(set(configured) | set(grants_by_server))
    return RetrievalStatusRead(
        project_id=project_id,
        lexical_available=True,
        vector_available=False,
        vector_namespace="harnessa_rag_v1",
        vector_omitted_reason="approved_embedding_route_not_configured",
        sources=source_reads,
        mcp_servers=[
            MCPServerStatusRead(
                server_id=server_id,
                configured=server_id in configured,
                granted=server_id in grants_by_server,
                protocol_version=grants_by_server[server_id].protocol_version if server_id in grants_by_server else None,
                available_tools=[],
                omitted_reason=(
                    "deployment_registry_missing"
                    if server_id not in configured
                    else "project_grant_absent"
                    if server_id not in grants_by_server
                    else "live_discovery_not_run"
                ),
            )
            for server_id in server_ids
        ],
        max_context_bytes=24_000,
        max_tool_output_bytes=MAX_TOOL_OUTPUT_BYTES,
    )


@router.post(
    "/projects/{project_id}/repositories/{repository_id}/retrieval/sync",
    response_model=RepositoryIndexSyncResponse,
)
async def sync_repository(
    project_id: UUID,
    repository_id: UUID,
    body: RepositoryIndexSyncRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    from harnessa.services.specifications import _workspace_manager

    return await sync_repository_index(
        db,
        project_id=project_id,
        repository_id=repository_id,
        actor=actor,
        manager=_workspace_manager,
        dry_run=body.dry_run,
    )


@router.post("/projects/{project_id}/mcp/grants", response_model=MCPGrantRead, status_code=201)
async def create_mcp_grant(
    project_id: UUID,
    body: MCPGrantCreate,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MCPGrantRead:
    membership = await require_membership(db, actor, project_id)
    if membership.role != "administrator":
        raise HTTPException(403, "Only project administrators may grant MCP access")
    configured = _configured_servers()
    if body.server_id not in configured:
        raise HTTPException(404, "MCP server is not deployment-allowlisted")
    configured_read_only = _configured_read_only_tools().get(body.server_id, set())
    if body.secret_reference and body.secret_reference not in _configured_secret_references().get(body.server_id, set()):
        raise HTTPException(422, "MCP secret reference is not deployment-approved for this server")
    if body.protocol_version not in SUPPORTED_MCP_PROTOCOLS:
        raise HTTPException(422, "Unsupported MCP protocol version")
    if not set(body.read_only_tools).issubset(body.allowed_tools):
        raise HTTPException(422, "Read-only tools must be included in the tool allowlist")
    if not set(body.read_only_tools).issubset(configured_read_only):
        raise HTTPException(422, "Read-only classifications must come from deployment policy")
    if len(set(body.allowed_tools)) != len(body.allowed_tools) or any(not name for name in body.allowed_tools):
        raise HTTPException(422, "Tool allowlist must contain unique non-empty names")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    grant = await db.scalar(
        select(MCPProjectGrant).where(
            MCPProjectGrant.project_id == project_id,
            MCPProjectGrant.server_id == body.server_id,
        )
    )
    if grant is None:
        grant = MCPProjectGrant(
            organization_id=project.organization_id,
            project_id=project_id,
            created_by=actor.id,
            server_id=body.server_id,
            allowed_tools=body.allowed_tools,
            read_only_tools=body.read_only_tools,
            protocol_version=body.protocol_version,
            secret_reference=body.secret_reference,
            enabled=True,
        )
        db.add(grant)
    else:
        grant.created_by = actor.id
        grant.allowed_tools = body.allowed_tools
        grant.read_only_tools = body.read_only_tools
        grant.protocol_version = body.protocol_version
        grant.secret_reference = body.secret_reference
        grant.enabled = True
        grant.revoked_at = None
    await db.commit()
    await db.refresh(grant)
    return _grant_read(grant)


@router.delete("/projects/{project_id}/mcp/grants/{grant_id}", status_code=204)
async def revoke_mcp_grant(
    project_id: UUID,
    grant_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    membership = await require_membership(db, actor, project_id)
    if membership.role != "administrator":
        raise HTTPException(403, "Only project administrators may revoke MCP access")
    grant = await db.scalar(
        select(MCPProjectGrant).where(
            MCPProjectGrant.id == grant_id,
            MCPProjectGrant.project_id == project_id,
        )
    )
    if grant is None:
        raise HTTPException(404, "MCP project grant not found")
    grant.enabled = False
    grant.revoked_at = datetime.now(UTC)
    await db.commit()


@router.post(
    "/projects/{project_id}/mcp/servers/{server_id}/tools/{tool_name}/invoke",
    response_model=MCPToolInvokeResponse,
)
async def invoke_mcp(
    project_id: UUID,
    server_id: str,
    tool_name: str,
    body: MCPToolInvokeRequest,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MCPToolInvokeResponse:
    started = time.monotonic()
    membership = await require_membership(db, actor, project_id)
    grant_row = await db.scalar(
        select(MCPProjectGrant).where(
            MCPProjectGrant.project_id == project_id,
            MCPProjectGrant.server_id == server_id,
            MCPProjectGrant.enabled.is_(True),
            MCPProjectGrant.revoked_at.is_(None),
        )
    )
    if grant_row is None:
        raise HTTPException(403, "MCP access is not granted for this project")
    grant = MCPGrant(
        organization_id=str(grant_row.organization_id),
        project_id=str(grant_row.project_id),
        server_id=grant_row.server_id,
        allowed_tools=frozenset(grant_row.allowed_tools),
        protocol_version=grant_row.protocol_version,
        secret_reference=grant_row.secret_reference,
    )
    grant_id = grant_row.id
    if tool_name not in grant_row.read_only_tools:
        raise HTTPException(403, "Tool is not explicitly granted as read-only")
    request_bytes = json.dumps(body.arguments, sort_keys=True, separators=(",", ":")).encode()
    if len(request_bytes) > 16_000:
        raise HTTPException(413, "MCP tool arguments exceed 16,000 bytes")
    secret_references = _configured_secret_references()
    if grant.secret_reference and grant.secret_reference not in secret_references.get(server_id, set()):
        raise HTTPException(403, "MCP secret reference is not deployment-approved for this server")
    client = StreamableHTTPMCPClient(_configured_servers(), secret_references=secret_references)
    request_hash = hashlib.sha256(request_bytes).hexdigest()
    response_hash: str | None = None
    outcome = "failed"
    error_code: str | None = None
    try:
        async def discover_and_invoke() -> bytes:
            discovered = await client.discover(
                server_id, grant.protocol_version, grant.secret_reference
            )
            selected = next((item for item in discovered if item.name == tool_name), None)
            if selected is None:
                raise HTTPException(404, "Tool is not advertised by this MCP server")
            tool = MCPTool(
                name=selected.name,
                description=selected.description,
                input_schema=selected.input_schema,
                read_only=True,
            )
            _validate_tool_arguments(tool.input_schema, body.arguments)
            return await invoke_mcp_tool(
                client,
                grant=grant,
                authenticated_project_id=str(project_id),
                discovered_tool=tool,
                arguments=body.arguments,
                server_protocol_version=grant.protocol_version,
                timeout_seconds=MAX_TOOL_TIMEOUT_SECONDS,
                output_limit_bytes=MAX_TOOL_OUTPUT_BYTES,
            )

        result = await asyncio.wait_for(discover_and_invoke(), timeout=MAX_TOOL_TIMEOUT_SECONDS)
        response_hash = hashlib.sha256(result).hexdigest()
        # Discard the original session and re-read current authorization before revealing source content.
        await db.rollback()
        await require_membership(db, actor, project_id)
        current_grant = await db.scalar(
            select(MCPProjectGrant).where(
                MCPProjectGrant.id == grant_id,
                MCPProjectGrant.project_id == project_id,
                MCPProjectGrant.enabled.is_(True),
                MCPProjectGrant.revoked_at.is_(None),
            )
        )
        if current_grant is None or tool_name not in current_grant.read_only_tools:
            raise HTTPException(403, "MCP grant was revoked during tool execution")
        outcome = "succeeded"
    except (ValueError, PermissionError, MCPRemoteError, TimeoutError) as exc:
        error_code = type(exc).__name__
        raise HTTPException(502, "MCP tool invocation failed policy or availability checks") from exc
    except HTTPException as exc:
        error_code = f"http_{exc.status_code}"
        raise
    finally:
        project = await db.get(Project, project_id)
        if project is not None:
            db.add(
                MCPToolAudit(
                    organization_id=project.organization_id,
                    project_id=project_id,
                    actor_id=actor.id,
                    server_id=server_id,
                    tool_name=tool_name,
                    protocol_version=grant.protocol_version,
                    outcome=outcome,
                    request_hash=request_hash,
                    response_hash=response_hash,
                    error_code=error_code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    metadata_json={"read_only": True, "role": membership.role},
                )
            )
            await db.commit()
    return MCPToolInvokeResponse(
        server_id=server_id,
        tool_name=tool_name,
        protocol_version=grant.protocol_version,
        result=result.decode("utf-8", errors="replace"),
        result_hash=response_hash or "0" * 64,
        untrusted_source_content=True,
    )


def _validate_tool_arguments(schema: dict[str, object], arguments: dict[str, object]) -> None:
    _validate_schema_value(schema, arguments, "arguments", depth=0)


def _validate_schema_value(
    schema: dict[str, object], value: object, path: str, *, depth: int
) -> None:
    if depth > 8:
        raise HTTPException(422, "MCP tool arguments exceed the 8-level nesting limit")
    if any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "not")):
        raise HTTPException(502, "MCP tool schema uses unsupported composition")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise HTTPException(422, f"MCP tool argument {path} must be an object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not all(isinstance(name, str) for name in properties)
            or not isinstance(required, list)
            or not all(isinstance(name, str) for name in required)
        ):
            raise HTTPException(502, "MCP tool object schema is malformed")
        if not set(value).issubset(properties) or not set(required).issubset(value):
            raise HTTPException(422, f"MCP tool argument {path} does not match the advertised schema")
        for name, nested_value in value.items():
            definition = properties.get(name)
            if not isinstance(definition, dict):
                raise HTTPException(502, "MCP tool property schema is malformed")
            _validate_schema_value(definition, nested_value, f"{path}.{name}", depth=depth + 1)
        return
    if expected == "array":
        if not isinstance(value, list):
            raise HTTPException(422, f"MCP tool argument {path} must be an array")
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            raise HTTPException(502, "MCP tool array schema must define items")
        if len(value) > 256:
            raise HTTPException(422, f"MCP tool argument {path} exceeds 256 array items")
        for index, nested_value in enumerate(value):
            _validate_schema_value(item_schema, nested_value, f"{path}[{index}]", depth=depth + 1)
        return
    type_checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected not in type_checks:
        raise HTTPException(502, "MCP tool schema contains an unsupported value type")
    if not type_checks[expected](value):
        raise HTTPException(422, f"MCP tool argument {path} must be a {expected}")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 16_000:
            raise HTTPException(422, f"MCP tool argument {path} exceeds 16,000 bytes")
        for constraint in ("minLength", "maxLength"):
            bound = schema.get(constraint)
            if bound is None:
                continue
            if not isinstance(bound, int) or isinstance(bound, bool):
                raise HTTPException(502, f"MCP tool schema has an invalid {constraint}")
            if (constraint == "minLength" and len(value) < bound) or (
                constraint == "maxLength" and len(value) > bound
            ):
                raise HTTPException(422, f"MCP tool argument {path} violates {constraint}")
        if "pattern" in schema:
            raise HTTPException(502, "MCP tool string patterns are not evaluated by this validator")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for constraint in ("minimum", "maximum"):
            bound = schema.get(constraint)
            if bound is None:
                continue
            if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                raise HTTPException(502, f"MCP tool schema has an invalid {constraint}")
            if (constraint == "minimum" and value < bound) or (
                constraint == "maximum" and value > bound
            ):
                raise HTTPException(422, f"MCP tool argument {path} violates {constraint}")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or value not in enum):
        raise HTTPException(422, f"MCP tool argument {path} is outside the advertised enum")
