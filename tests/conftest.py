"""Optional real-service integration configuration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL")
if not os.getenv("OPENSPEC_NODE_PATH"):
    node_launcher = shutil.which("node")
    if node_launcher:
        node_path = subprocess.run(
            [node_launcher, "-p", "process.execPath"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        os.environ["OPENSPEC_NODE_PATH"] = node_path
if TEST_DATABASE_URL:
    database_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0]
    if not database_name.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must point to a database ending in _test")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
if TEST_REDIS_URL:
    redis_database = urlsplit(TEST_REDIS_URL).path.lstrip("/") or "0"
    if redis_database == "0":
        raise RuntimeError("TEST_REDIS_URL must use a dedicated non-zero Redis database")
    os.environ["REDIS_URL"] = TEST_REDIS_URL


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database():
    if not TEST_DATABASE_URL:
        return
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["DATABASE_URL"] = TEST_DATABASE_URL
    environment["PYTHONPATH"] = str(root / "apps/api")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root / "apps/api",
        env=environment,
        check=True,
    )
