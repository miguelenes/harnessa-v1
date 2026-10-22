from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession
from harnessa.config import settings
from harnessa.db.models import User
from harnessa.db.session import get_db


async def get_user_db(session: AsyncSession = Depends(get_db)):
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(UUIDIDMixin, BaseUserManager[User, __import__("uuid").UUID]):
    reset_password_token_secret = settings.secret_key or "development-only-change-this-secret-key"
    verification_token_secret = reset_password_token_secret


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


cookie_transport = CookieTransport(
    cookie_name="harnessa_auth",
    cookie_max_age=3600,
    cookie_httponly=True,
    cookie_secure=settings.cookie_secure,
    cookie_samesite="lax",
)


def get_jwt_strategy():
    return JWTStrategy(
        secret=settings.secret_key or "development-only-change-this-secret-key",
        lifetime_seconds=3600,
    )


auth_backend = AuthenticationBackend(
    name="cookie", transport=cookie_transport, get_strategy=get_jwt_strategy
)
fastapi_users = FastAPIUsers[User, __import__("uuid").UUID](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)
