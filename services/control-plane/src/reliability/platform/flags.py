"""Feature flags and the operator kill switch (operations.md §2, threat model A1)."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KILL_SWITCH = "kill_switch"


async def is_enabled(session: AsyncSession, key: str, org_id: uuid.UUID | None = None, default: bool = False) -> bool:
    """Org-specific value wins over the global one."""
    rows = (
        await session.execute(
            text("SELECT org_id, enabled FROM feature_flags WHERE key = :k AND (org_id IS NULL OR org_id = :o)"),
            {"k": key, "o": org_id},
        )
    ).all()
    values = {r.org_id: r.enabled for r in rows}
    if org_id in values:
        return bool(values[org_id])
    if None in values:
        return bool(values[None])
    return default


async def kill_switch_active(session: AsyncSession, org_id: uuid.UUID | None) -> bool:
    return await is_enabled(session, KILL_SWITCH, None) or (
        org_id is not None and await is_enabled(session, KILL_SWITCH, org_id)
    )


async def set_flag(session: AsyncSession, key: str, enabled: bool, org_id: uuid.UUID | None = None) -> None:
    """System session only (feature_flags is read-only for the app role)."""
    await session.execute(
        text(
            "INSERT INTO feature_flags (key, org_id, enabled) VALUES (:k, :o, :e) "
            "ON CONFLICT (key, org_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()"
        ),
        {"k": key, "o": org_id, "e": enabled},
    )
