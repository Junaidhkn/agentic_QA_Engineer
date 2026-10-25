"""Idempotency-Key handling for creating POSTs (api-contract.md §1).

Usage inside an endpoint (same transaction as the work)::

    cached = await idempotency.lookup(session, org_id, key, body)
    if cached: return cached.to_response()
    ... do work ...
    await idempotency.store(session, org_id, key, body, status, payload)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.platform.errors import AppError

TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Cached:
    status_code: int
    response: Any

    def to_response(self) -> JSONResponse:
        return JSONResponse(self.response, status_code=self.status_code, headers={"Idempotent-Replayed": "true"})


def _hash(body: Any) -> bytes:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).digest()


def require_key(key: str | None) -> str:
    if not key or not (8 <= len(key) <= 128):
        raise AppError(
            "VALIDATION_FAILED", "Idempotency-Key header (8-128 chars) is required", {"header": "Idempotency-Key"}
        )
    return key


async def lookup(session: AsyncSession, org_id: uuid.UUID, key: str, body: Any) -> Cached | None:
    # Serialize concurrent requests with the same key.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"idem:{org_id}:{key}"})
    row = (
        await session.execute(
            text(
                "SELECT request_hash, response, status_code, expires_at FROM idempotency_keys "
                "WHERE org_id = :o AND key = :k"
            ),
            {"o": org_id, "k": key},
        )
    ).first()
    if row is None or row.expires_at < datetime.now(UTC):
        return None
    if row.request_hash != _hash(body):
        raise AppError("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used with a different request body")
    return Cached(row.status_code, row.response)


async def store(session: AsyncSession, org_id: uuid.UUID, key: str, body: Any, status_code: int, response: Any) -> None:
    await session.execute(
        text(
            """
            INSERT INTO idempotency_keys (org_id, key, request_hash, response, status_code, expires_at)
            VALUES (:o, :k, :h, CAST(:r AS jsonb), :s, :e)
            ON CONFLICT (org_id, key) DO UPDATE SET request_hash = EXCLUDED.request_hash,
              response = EXCLUDED.response, status_code = EXCLUDED.status_code, expires_at = EXCLUDED.expires_at
            """
        ),
        {
            "o": org_id,
            "k": key,
            "h": _hash(body),
            "r": json.dumps(response, default=str),
            "s": status_code,
            "e": datetime.now(UTC) + TTL,
        },
    )
