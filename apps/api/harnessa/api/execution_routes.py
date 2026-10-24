"""Project-scoped selection of the installed execution adapter."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from harnessa.auth.users import current_active_user
from harnessa.config import settings
from harnessa.db.models import Project, User
from harnessa.db.session import get_db
from harnessa.services.authorization import require_membership
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["execution setup"])
AdapterName = Literal["deployment-default", "acp-opencode", "disabled"]


class ExecutionAdapterWrite(BaseModel):
    adapter: AdapterName


class ExecutionAdapterRead(BaseModel):
    project_id: UUID
    adapter: AdapterName
    available_adapters: list[AdapterName]
    configured: bool
    detail: str


def _read(project: Project) -> ExecutionAdapterRead:
    available: list[AdapterName] = ["deployment-default", "disabled"]
    if settings.acp_execution_enabled:
        available.insert(1, "acp-opencode")
    selected_available = project.execution_adapter != "acp-opencode" or settings.acp_execution_enabled
    deployment_available = settings.acp_execution_enabled
    configured = False
    if project.execution_adapter == "disabled":
        detail = "Agent execution is disabled for this project."
    elif not selected_available:
        detail = "ACP/OpenCode is selected, but its deployment runtime is unavailable."
    elif project.execution_adapter == "deployment-default":
        detail = (
            "This project uses the deployment-configured runner."
            if deployment_available else
            "This project uses the deployment default, but no execution runner is enabled."
        )
        configured = deployment_available
    else:
        detail = "ACP through the pinned OpenCode adapter is selected for this project."
        configured = selected_available
    return ExecutionAdapterRead(
        project_id=project.id,
        adapter=project.execution_adapter,  # type: ignore[arg-type]
        available_adapters=available,
        configured=configured,
        detail=detail,
    )


@router.get("/projects/{project_id}/execution-adapter", response_model=ExecutionAdapterRead)
async def get_execution_adapter(
    project_id: UUID,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExecutionAdapterRead:
    await require_membership(db, actor, project_id)
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return _read(project)


@router.put("/projects/{project_id}/execution-adapter", response_model=ExecutionAdapterRead)
async def set_execution_adapter(
    project_id: UUID,
    body: ExecutionAdapterWrite,
    actor: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ExecutionAdapterRead:
    membership = await require_membership(db, actor, project_id)
    if getattr(membership, "role", "") not in {"administrator", "operator"}:
        raise HTTPException(403, "Project operator role required to select an execution adapter")
    project = await db.get(Project, project_id, with_for_update=True)
    if project is None:
        raise HTTPException(404, "Project not found")
    if body.adapter == "acp-opencode" and not settings.acp_execution_enabled:
        raise HTTPException(409, "The ACP/OpenCode runner is not enabled in this deployment")
    project.execution_adapter = body.adapter
    await db.commit()
    await db.refresh(project)
    return _read(project)
