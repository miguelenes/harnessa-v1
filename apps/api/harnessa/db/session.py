import os
from collections.abc import AsyncIterator

from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from harnessa.config import settings

engine_options: dict[str, object] = {"pool_pre_ping": True}
if os.getenv("TEST_DATABASE_URL"):
    # Integration tests use AnyIO runner loops per test; do not retain asyncpg
    # connections across those loops.
    engine_options["poolclass"] = NullPool
engine = create_async_engine(settings.database_url, **engine_options)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
