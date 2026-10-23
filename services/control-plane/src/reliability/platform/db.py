"""Database engines and tenant-scoped transactions (ADR-0002, ADR-0003).

Two roles are used at runtime:

* ``re_app`` — every request and tenant job. Row-level security is enforced; the visible
  tenants are set per transaction with ``set_config('app.org_ids', …, true)``.
* ``re_system`` — BYPASSRLS, only for cross-tenant platform jobs (outbox relay, lease
  acquisition, reapers, retention). Use :func:`system_tx`; its use is restricted to
  ``platform`` and the modules listed in ``SYSTEM_SESSION_ALLOWED`` (checked by a test).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from reliability.platform.settings import get_settings

SYSTEM_SESSION_ALLOWED = (
    "reliability.platform",
    "reliability.modules.runners",
    "reliability.modules.runs",
    "reliability.modules.targets",
    "reliability.modules.retention",
    "reliability.modules.integrations.github",
    "reliability.modules.identity",
    "reliability.modules.tenancy",
    "reliability.modules.audit",
    "reliability.modules.notifications",
    "reliability.cli",
    "reliability.modules.telemetry",
)


class Base(DeclarativeBase):
    pass


_engines: dict[str, AsyncEngine] = {}
_nullpool = False


def use_nullpool(enabled: bool = True) -> None:
    """Workers run handlers in short-lived event loops; pooled connections must not cross loops."""
    global _nullpool
    _nullpool = enabled


def _make_engine(url: str) -> AsyncEngine:
    settings = get_settings()
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if _nullpool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.database_pool_size
        kwargs["max_overflow"] = settings.database_pool_size
    return create_async_engine(url, **kwargs)


def app_engine() -> AsyncEngine:
    if "app" not in _engines:
        _engines["app"] = _make_engine(get_settings().database_url)
    return _engines["app"]


def system_engine() -> AsyncEngine:
    if "system" not in _engines:
        _engines["system"] = _make_engine(get_settings().database_system_url)
    return _engines["system"]


async def dispose_engines() -> None:
    for engine in list(_engines.values()):
        await engine.dispose()
    _engines.clear()


async def set_tenant(session: AsyncSession, user_id: uuid.UUID | None, org_ids: Iterable[uuid.UUID]) -> None:
    await session.execute(
        text("select set_config('app.user_id', :u, true), set_config('app.org_ids', :o, true)"),
        {"u": str(user_id) if user_id else "", "o": ",".join(str(o) for o in org_ids)},
    )


@asynccontextmanager
async def tenant_tx(
    *, user_id: uuid.UUID | None = None, org_ids: Iterable[uuid.UUID] = ()
) -> AsyncIterator[AsyncSession]:
    """One transaction with RLS scoped to ``org_ids`` (and memberships of ``user_id``)."""
    async with AsyncSession(app_engine(), expire_on_commit=False) as session:
        async with session.begin():
            await set_tenant(session, user_id, org_ids)
            yield session


@asynccontextmanager
async def system_tx() -> AsyncIterator[AsyncSession]:
    """Cross-tenant transaction (BYPASSRLS). Restricted use — see module docstring."""
    async with AsyncSession(system_engine(), expire_on_commit=False) as session:
        async with session.begin():
            yield session


async def ping() -> bool:
    try:
        async with app_engine().connect() as conn:
            await conn.execute(text("select 1"))
        return True
    except Exception:
        return False
