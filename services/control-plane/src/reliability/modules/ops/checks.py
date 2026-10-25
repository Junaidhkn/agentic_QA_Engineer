"""Latest outcome of each platform check (``platform_checks``), read by the metrics endpoint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CANARY = "canary"
INTEGRITY = "integrity"
RESTORE_DRILL = "restore_drill"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: dict[str, Any]
    checked_at: datetime
    last_success_at: datetime | None
    consecutive_failures: int


async def record(session: AsyncSession, name: str, ok: bool, detail: dict[str, Any]) -> None:
    """Upsert the check's outcome (system session)."""
    await session.execute(
        text(
            """
            INSERT INTO platform_checks (name, status, detail, checked_at, last_success_at, consecutive_failures)
            VALUES (:n, :s, CAST(:d AS jsonb), now(), CASE WHEN :ok THEN now() END, CASE WHEN :ok THEN 0 ELSE 1 END)
            ON CONFLICT (name) DO UPDATE SET
              status = EXCLUDED.status,
              detail = EXCLUDED.detail,
              checked_at = now(),
              last_success_at = CASE WHEN :ok THEN now() ELSE platform_checks.last_success_at END,
              consecutive_failures = CASE WHEN :ok THEN 0 ELSE platform_checks.consecutive_failures + 1 END
            """
        ),
        {"n": name, "s": "ok" if ok else "failed", "d": json.dumps(detail, default=str), "ok": ok},
    )


async def mark_running(session: AsyncSession, name: str, detail: dict[str, Any]) -> None:
    """First firing of a check whose outcome is only known later; keeps ``last_success_at``."""
    await session.execute(
        text(
            "INSERT INTO platform_checks (name, status, detail) VALUES (:n, 'running', CAST(:d AS jsonb)) "
            "ON CONFLICT (name) DO UPDATE SET status = 'running', detail = EXCLUDED.detail, checked_at = now()"
        ),
        {"n": name, "d": json.dumps(detail, default=str)},
    )


async def get(session: AsyncSession, name: str) -> Check | None:
    row = (await session.execute(text("SELECT * FROM platform_checks WHERE name = :n"), {"n": name})).first()
    return Check(**row._asdict()) if row else None


async def all_checks(session: AsyncSession) -> list[Check]:
    rows = (await session.execute(text("SELECT * FROM platform_checks ORDER BY name"))).all()
    return [Check(**r._asdict()) for r in rows]
