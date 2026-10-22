import hmac
import secrets
from uuid import UUID
import hashlib
import json
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi_users import schemas
from harnessa.config import settings
from harnessa.db.models import (
    User,
    Organization,
    Membership,
    ProjectMembership,
    Project,
    Repository,
    IdempotencyRecord,
)
from harnessa.db.session import get_db
from harnessa.db.model_gateway import ModelPool
from harnessa.auth.users import current_active_user, fastapi_users, auth_backend
from harnessa.services.authorization import require_membership
from harnessa.contracts import (
    ErrorResponse,
    Identity,
    ProjectCreate,
    ProjectRead,
    RepositoryCreate,
    RepositoryRead,
)

router = APIRouter()
router.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/cookie", tags=["auth"]
)


class BootstrapUser(schemas.BaseUserCreate):
    password: str = __import__("pydantic").Field(min_length=12, max_length=128)
    organization_name: str = "Harnessa"


@router.post("/auth/bootstrap", status_code=201)
async def bootstrap(body: BootstrapUser, request: Request, db: AsyncSession = Depends(get_db)):
    key = request.headers.get("x-bootstrap-token", "")
    if not settings.bootstrap_token or not hmac.compare_digest(key, settings.bootstrap_token):
        raise HTTPException(404, "Not found")
    from sqlalchemy import text

    await db.execute(text("SELECT pg_advisory_xact_lock(987654321)"))
    if await db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "Administrator already bootstrapped")
    from pwdlib import PasswordHash

    user = User(
        email=str(body.email).lower(),
        hashed_password=PasswordHash.recommended().hash(body.password),
        is_superuser=True,
    )
    org = Organization(name=body.organization_name)
    db.add_all([user, org])
    await db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="administrator"))
    db.add_all(
        [
            ModelPool(
                organization_id=org.id,
                name="interactive/primary",
                kind="interactive",
                max_concurrency=settings.model_interactive_concurrency,
                interactive_reserve=settings.model_interactive_reserve,
            ),
            ModelPool(
                organization_id=org.id,
                name="probe/batch",
                kind="probe",
                max_concurrency=settings.model_interactive_concurrency,
                interactive_reserve=settings.model_interactive_reserve,
                probe_overflow_limit=settings.model_probe_overflow_limit,
            ),
        ]
    )
    await db.commit()
    return {"id": str(user.id), "email": user.email, "organization_id": str(org.id)}


@router.post("/auth/logout", status_code=204)
async def logout(response: Response):
    response.delete_cookie("harnessa_auth", path="/")
    response.delete_cookie("harnessa_csrf", path="/")


@router.get("/auth/csrf")
async def csrf(response: Response):
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        "harnessa_csrf",
        token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"csrf_token": token}


@router.get("/auth/me", response_model=Identity, responses={401: {"model": ErrorResponse}})
async def identity(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)):
    member = await require_membership(db, user)
    return Identity(
        id=user.id, email=user.email, organization_id=member.organization_id, role=member.role
    )


@router.get("/projects", response_model=list[ProjectRead])
async def projects(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)):
    memberships = (
        (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalars().all()
    )
    org_ids = [m.organization_id for m in memberships]
    if not org_ids:
        return []
    admin_orgs = [m.organization_id for m in memberships if m.role == "administrator"]
    org_projects = select(Project.id).where(Project.organization_id.in_(admin_orgs))
    member_projects = (
        select(ProjectMembership.project_id)
        .join(Project, Project.id == ProjectMembership.project_id)
        .where(ProjectMembership.user_id == user.id, Project.organization_id.in_(org_ids))
    )
    project_ids = (await db.execute(org_projects.union(member_projects))).scalars().all()
    if not project_ids:
        return []
    return (await db.execute(select(Project).where(Project.id.in_(project_ids)))).scalars().all()


async def replay_or_conflict(db: AsyncSession, user: User, key: str, payload: dict):
    if not key or len(key) > 200:
        raise HTTPException(400, "A valid Idempotency-Key is required")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"{user.id}:{key}"},
    )
    existing = (
        (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.user_id == user.id, IdempotencyRecord.key == key
                )
            )
        )
        .scalars()
        .first()
    )
    if not existing:
        return digest, None
    if existing.request_hash != digest:
        raise HTTPException(409, "Idempotency-Key was already used with a different request")
    return digest, JSONResponse(
        existing.response_body,
        status_code=existing.response_status,
        headers={"Idempotency-Replayed": "true"},
    )


async def save_idempotency(
    db: AsyncSession, user: User, key: str, digest: str, status: int, response: dict
):
    db.add(
        IdempotencyRecord(
            user_id=user.id,
            key=key,
            request_hash=digest,
            response_status=status,
            response_body=response,
        )
    )


@router.post(
    "/projects",
    response_model=ProjectRead,
    status_code=201,
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_project(
    body: ProjectCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
):
    m = await require_membership(db, user)
    if m.role not in ("administrator", "operator"):
        raise HTTPException(403, "Insufficient role")
    digest, replay = await replay_or_conflict(
        db, user, idempotency_key, {"operation": "create_project", **body.model_dump()}
    )
    if replay:
        return replay
    project = Project(organization_id=m.organization_id, name=body.name, slug=body.slug)
    db.add(project)
    try:
        await db.flush()
        db.add(
            ProjectMembership(
                project_id=project.id,
                user_id=user.id,
                role="administrator" if m.role == "administrator" else "operator",
            )
        )
        await db.flush()
        await save_idempotency(
            db,
            user,
            idempotency_key,
            digest,
            201,
            ProjectRead.model_validate(project).model_dump(mode="json"),
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Project slug already exists") from exc
    return project


@router.get("/projects/{project_id}/repositories", response_model=list[RepositoryRead])
async def repositories(
    project_id: UUID, user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
):
    await require_membership(db, user, project_id)
    return (
        (await db.execute(select(Repository).where(Repository.project_id == project_id)))
        .scalars()
        .all()
    )


@router.post(
    "/projects/{project_id}/repositories",
    response_model=RepositoryRead,
    status_code=201,
    responses={
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def add_repository(
    project_id: UUID,
    body: RepositoryCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
):
    m = await require_membership(db, user, project_id)
    if m.role not in ("administrator", "operator"):
        raise HTTPException(403, "Insufficient role")
    digest, replay = await replay_or_conflict(
        db,
        user,
        idempotency_key,
        {"operation": "register_repository", "project_id": str(project_id), **body.model_dump()},
    )
    if replay:
        return replay
    targets = {s.strip() for s in settings.repository_targets.split(",") if s.strip()}
    if body.target_id not in targets:
        raise HTTPException(422, "Repository target is not explicitly configured")
    repository = Repository(
        project_id=project_id,
        target_id=body.target_id,
        display_name=body.display_name,
        default_branch=body.default_branch,
    )
    db.add(repository)
    try:
        await db.flush()
        await save_idempotency(
            db,
            user,
            idempotency_key,
            digest,
            201,
            RepositoryRead.model_validate(repository).model_dump(mode="json"),
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(409, "Repository already registered") from exc
    return repository
