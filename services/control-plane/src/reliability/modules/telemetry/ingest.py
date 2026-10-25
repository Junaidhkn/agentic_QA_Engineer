"""Authentication for OTLP ingest (FR-23, ADR-0014).

Two token types are accepted by the OTLP gateway:
* run tokens (JWT, audience ``otlp``) minted per lease for sandboxes and generators;
* project ingest tokens (``rit_<prefix>_<secret>``, hashed at rest) that customers configure
  in their staging app's OpenTelemetry exporter.
The gateway — never the client — decides the tenant header.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.telemetry.models import IngestToken
from reliability.platform.crypto import constant_time_equals, hmac_digest, verify_token
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

PREFIX = "rit_"
_CACHE: dict[str, tuple[float, IngestIdentity | None]] = {}
CACHE_TTL_S = 60


@dataclass(frozen=True)
class IngestIdentity:
    org_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None


async def create(
    session: AsyncSession, *, org_id: uuid.UUID, project_id: uuid.UUID, name: str, created_by: uuid.UUID
) -> tuple[IngestToken, str]:
    prefix = f"{PREFIX}{secrets.token_hex(4)}"
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    row = IngestToken(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        name=name,
        token_prefix=prefix,
        token_hash=hmac_digest(token),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row, token


async def authenticate(token: str) -> IngestIdentity:
    if not token.startswith(PREFIX):
        claims = verify_token(token, "otlp")
        return IngestIdentity(
            uuid.UUID(claims["org"]),
            uuid.UUID(claims["project"]),
            uuid.UUID(claims["run"]) if claims.get("run") else None,
        )
    cached = _CACHE.get(token)
    if cached and cached[0] > time.monotonic():
        if cached[1] is None:
            raise AppError("UNAUTHENTICATED", "invalid ingest token")
        return cached[1]
    try:
        _, part, _secret = token.split("_", 2)
    except ValueError as exc:
        raise AppError("UNAUTHENTICATED", "invalid ingest token") from exc
    async with system_tx() as session:
        row = (
            await session.execute(select(IngestToken).where(IngestToken.token_prefix == f"{PREFIX}{part}"))
        ).scalar_one_or_none()
    identity = None
    if row is not None and row.revoked_at is None and constant_time_equals(row.token_hash, hmac_digest(token)):
        identity = IngestIdentity(row.org_id, row.project_id, None)
    _CACHE[token] = (time.monotonic() + CACHE_TTL_S, identity)
    if identity is None:
        raise AppError("UNAUTHENTICATED", "invalid ingest token")
    return identity


async def revoke(session: AsyncSession, project_id: uuid.UUID, token_id: uuid.UUID) -> bool:
    row = await session.get(IngestToken, token_id)
    if row is None or row.project_id != project_id or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    _CACHE.clear()
    return True
