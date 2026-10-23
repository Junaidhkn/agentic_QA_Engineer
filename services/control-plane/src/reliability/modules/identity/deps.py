"""FastAPI dependencies: authenticate the caller and open one tenant-scoped transaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.identity.service import Principal, authenticate
from reliability.modules.quotas import ratelimit
from reliability.platform.actor import Actor
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError


@dataclass
class Ctx:
    principal: Principal
    session: AsyncSession
    request: Request

    @property
    def actor(self) -> Actor:
        return self.principal.actor


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    base = Actor(
        "user",
        "anonymous",
        _client_ip(request),
        request.headers.get("User-Agent"),
        getattr(request.state, "request_id", None),
    )
    try:
        return await authenticate(authorization, base)
    except AppError as exc:
        if exc.code == "UNAUTHENTICATED":
            await ratelimit.check(base.ip or "unknown", "auth")
        raise


async def get_ctx(request: Request, principal: Annotated[Principal, Depends(get_principal)]) -> AsyncIterator[Ctx]:
    async with tenant_tx(user_id=principal.user_id, org_ids=principal.org_ids) as session:
        yield Ctx(principal, session, request)


CtxDep = Annotated[Ctx, Depends(get_ctx)]
