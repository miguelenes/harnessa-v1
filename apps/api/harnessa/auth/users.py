from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import UUID

from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.models import UserProtocol
from harnessa.config import settings
from harnessa.db.models import User
from harnessa.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession


async def get_user_db(
    session: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SQLAlchemyUserDatabase[UserProtocol[UUID], UUID], None]:
    model = cast(type[UserProtocol[UUID]], User)
    yield SQLAlchemyUserDatabase(session, model)


class UserManager(UUIDIDMixin, BaseUserManager[UserProtocol[UUID], UUID]):
    reset_password_token_secret = settings.secret_key or "development-only-change-this-secret-key"
    verification_token_secret = reset_password_token_secret


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase[UserProtocol[UUID], UUID] = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


cookie_transport = CookieTransport(
    cookie_name="harnessa_auth",
    cookie_max_age=3600,
    cookie_httponly=True,
    cookie_secure=settings.cookie_secure,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy[UserProtocol[UUID], UUID]:
    return JWTStrategy(
        secret=settings.secret_key or "development-only-change-this-secret-key",
        lifetime_seconds=3600,
    )


auth_backend = AuthenticationBackend[UserProtocol[UUID], UUID](
    name="cookie", transport=cookie_transport, get_strategy=get_jwt_strategy
)
fastapi_users = FastAPIUsers[UserProtocol[UUID], UUID](get_user_manager, [auth_backend])
current_active_user = cast(Any, fastapi_users.current_user(active=True))
