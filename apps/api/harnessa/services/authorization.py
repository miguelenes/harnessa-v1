"""Organization and project membership checks shared by protected API operations."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harnessa.db.models import Membership, Project, ProjectMembership, User


async def require_membership(
    db: AsyncSession, user: User, project_id: UUID | None = None
) -> Membership | ProjectMembership:
    query = select(Membership).where(Membership.user_id == user.id)
    if project_id:
        query = query.join(Project, Project.organization_id == Membership.organization_id).where(
            Project.id == project_id
        )
    organization_membership = (await db.execute(query)).scalars().first()
    if not organization_membership:
        raise HTTPException(403, "Organization access denied")
    if project_id is None or organization_membership.role == "administrator":
        return organization_membership

    project_membership = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user.id,
            )
        )
    ).scalars().first()
    if not project_membership:
        raise HTTPException(403, "Project access denied")
    return project_membership
