from __future__ import annotations

import os

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from harnessa.config import settings
from harnessa.db.session import SessionLocal


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def isolated_real_services(anyio_backend):
    if not os.getenv("TEST_DATABASE_URL") or not os.getenv("TEST_REDIS_URL"):
        pytest.skip("Set TEST_DATABASE_URL and TEST_REDIS_URL to disposable PostgreSQL/Redis services")
    async with SessionLocal() as db:
        await db.execute(text("TRUNCATE TABLE organizations, users CASCADE"))
        await db.commit()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await redis.flushdb()
    yield
    await redis.flushdb()
    await redis.aclose()
