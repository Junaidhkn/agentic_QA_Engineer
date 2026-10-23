"""Test harness: a fresh ``reliability_test`` database migrated to head, real Redis (db 15),
local object store in a temp dir, fake LLM. Tables are truncated between tests."""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import psycopg
import pytest

SUPERUSER_URL = os.environ.get("RE_TEST_PG_SUPERUSER_URL", "postgresql://postgres@localhost:5432/postgres")
TEST_DB = "reliability_test"
_tmp = tempfile.mkdtemp(prefix="re-test-")

os.environ.update(
    {
        "RE_ENV": "test",
        "RE_DATABASE_URL": f"postgresql+psycopg://re_app:re_app@localhost:5432/{TEST_DB}",
        "RE_DATABASE_SYSTEM_URL": f"postgresql+psycopg://re_system:re_system@localhost:5432/{TEST_DB}",
        "RE_DATABASE_MIGRATION_URL": f"postgresql+psycopg://re_owner:re_owner@localhost:5432/{TEST_DB}",
        "RE_REDIS_URL": "redis://localhost:6379/15",
        "RE_OBJECT_STORE": "local",
        "RE_LOCAL_STORE_PATH": os.path.join(_tmp, "store"),
        "RE_LLM_PROVIDER": "fake",
        "RE_DEV_AUTH_ENABLED": "true",
        "RE_API_BASE_URL": "http://testserver",
    }
)

from reliability.platform.settings import get_settings  # noqa: E402

get_settings.cache_clear()


def _superuser(dbname: str = "postgres") -> psycopg.Connection:
    return psycopg.connect(SUPERUSER_URL.rsplit("/", 1)[0] + "/" + dbname, autocommit=True)


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    with _superuser() as conn:
        conn.execute(
            open(os.path.join(os.path.dirname(__file__), "../../../scripts/db-bootstrap.sql"))
            .read()
            .split("SELECT 'CREATE DATABASE")[0]
        )
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB} OWNER re_owner")
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    yield


_KEEP = {"plans", "alembic_version"}


@pytest.fixture(autouse=True)
def clean_tables(database: None) -> Iterator[None]:
    yield
    with _superuser(TEST_DB) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename NOT IN (SELECT inhrelid::regclass::text FROM pg_inherits)"
            )
            if r[0] not in _KEEP
        ]
        conn.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    import redis

    redis.Redis.from_url(os.environ["RE_REDIS_URL"]).flushdb()


@pytest.fixture(autouse=True)
async def _dispose_engines() -> AsyncIterator[None]:
    yield
    from reliability.platform import db, redis_client

    await db.dispose_engines()
    await redis_client.close()


@dataclass
class OrgFixture:
    org_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    project_id: uuid.UUID


def make_org(name: str, plan: str = "startup", role: str = "owner") -> OrgFixture:
    """Create org + owner + one project directly in SQL (superuser), independent of app code."""
    org_id, user_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    email = f"{name}@example.com"
    with _superuser(TEST_DB) as conn:
        conn.execute(
            "INSERT INTO organizations (id, name, slug, plan_id) VALUES (%s,%s,%s,%s)",
            (org_id, name, f"{name}-org", plan),
        )
        conn.execute("INSERT INTO users (id, idp_subject, email) VALUES (%s,%s,%s)", (user_id, f"dev:{email}", email))
        conn.execute("INSERT INTO memberships (org_id, user_id, role) VALUES (%s,%s,%s)", (org_id, user_id, role))
        conn.execute("INSERT INTO projects (id, org_id, name) VALUES (%s,%s,%s)", (project_id, org_id, "app"))
    return OrgFixture(org_id, user_id, email, project_id)


def sql(query: str, *params: object) -> list[tuple]:
    with _superuser(TEST_DB) as conn:
        cur = conn.execute(query, params or None)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def org_a() -> OrgFixture:
    return make_org("alpha")


@pytest.fixture
def org_b() -> OrgFixture:
    return make_org("bravo")


@pytest.fixture
async def api() -> AsyncIterator[object]:
    import httpx

    from reliability.main_api import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def auth(email: str, org_id: uuid.UUID | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer dev:{email}"}
    if org_id:
        headers["X-Org-Id"] = str(org_id)
    return headers


def add_member(org: OrgFixture, email: str, role: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    with _superuser(TEST_DB) as conn:
        conn.execute("INSERT INTO users (id, idp_subject, email) VALUES (%s,%s,%s)", (user_id, f"dev:{email}", email))
        conn.execute("INSERT INTO memberships (org_id, user_id, role) VALUES (%s,%s,%s)", (org.org_id, user_id, role))
    return user_id
