"""Plan limits and monthly quotas with atomic reservations (FR-31, ADR-0024).

Accounting: ``reserve(+R)`` at admission; at completion ``settle`` writes ``release(-R)`` and
``commit(+A)``, so the period sum equals actual usage. Reservations are serialized per org
with a transaction-scoped advisory lock, so concurrent admissions cannot overspend.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.quotas.models import Plan, UsageEntry
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

UsageKind = Literal["vu_minutes", "sandbox_minutes", "llm_tokens", "runs"]

_LIMIT_FIELD: dict[str, str] = {
    "vu_minutes": "monthly_vu_minutes",
    "sandbox_minutes": "monthly_sandbox_minutes",
    "llm_tokens": "monthly_llm_tokens",
}


def current_period(now: datetime | None = None) -> date:
    now = now or datetime.now(UTC)
    return date(now.year, now.month, 1)


def next_period(period: date) -> date:
    return date(period.year + (period.month == 12), period.month % 12 + 1, 1)


async def get_plan(session: AsyncSession, org_id: uuid.UUID) -> Plan:
    plan = (
        await session.execute(
            select(Plan).from_statement(
                text("SELECT plans.* FROM plans JOIN organizations o ON o.plan_id = plans.id WHERE o.id = :org")
            ),
            {"org": org_id},
        )
    ).scalar_one_or_none()
    if plan is None:
        raise AppError("NOT_FOUND", "organization not found")
    return plan


async def used(session: AsyncSession, org_id: uuid.UUID, kind: UsageKind, period: date | None = None) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(UsageEntry.quantity), 0)).where(
            UsageEntry.org_id == org_id, UsageEntry.kind == kind, UsageEntry.period == (period or current_period())
        )
    )
    return int(total or 0)


async def _lock(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"quota:{org_id}"})


def _entry(org_id: uuid.UUID, kind: str, qty: int, entry_type: str, subject_id: uuid.UUID | None) -> UsageEntry:
    return UsageEntry(
        id=uuid7(),
        org_id=org_id,
        period=current_period(),
        kind=kind,
        quantity=qty,
        entry_type=entry_type,
        subject_id=subject_id,
        created_at=datetime.now(UTC),
    )


async def reserve(
    session: AsyncSession, org_id: uuid.UUID, kind: UsageKind, quantity: int, subject_id: uuid.UUID | None = None
) -> None:
    if quantity < 0:
        raise ValueError("quantity must be >= 0")
    await _lock(session, org_id)
    if kind in _LIMIT_FIELD:
        plan = await get_plan(session, org_id)
        limit = int(getattr(plan, _LIMIT_FIELD[kind]))
        current = await used(session, org_id, kind)
        if current + quantity > limit:
            period = current_period()
            raise AppError(
                "QUOTA_EXCEEDED",
                f"monthly {kind.replace('_', ' ')} quota exhausted",
                {
                    "kind": kind,
                    "limit": limit,
                    "used": current,
                    "requested": quantity,
                    "resets_at": datetime.combine(next_period(period), datetime.min.time(), UTC).isoformat(),
                },
            )
    session.add(_entry(org_id, kind, quantity, "reserve", subject_id))
    await session.flush()


async def settle(
    session: AsyncSession,
    org_id: uuid.UUID,
    kind: UsageKind,
    *,
    reserved: int,
    actual: int,
    subject_id: uuid.UUID | None = None,
) -> None:
    """Replace a reservation with actual usage (actual may be 0, e.g. cancelled before start)."""
    await _lock(session, org_id)
    if reserved:
        session.add(_entry(org_id, kind, -reserved, "release", subject_id))
    if actual:
        session.add(_entry(org_id, kind, actual, "commit", subject_id))
    await session.flush()


async def record(
    session: AsyncSession, org_id: uuid.UUID, kind: UsageKind, quantity: int, subject_id: uuid.UUID | None = None
) -> None:
    """Record usage that was not reserved in advance (e.g. LLM tokens metered per call)."""
    await _lock(session, org_id)
    session.add(_entry(org_id, kind, quantity, "commit", subject_id))
    await session.flush()


async def usage_summary(session: AsyncSession, org_id: uuid.UUID) -> dict[str, dict[str, int]]:
    plan = await get_plan(session, org_id)
    out: dict[str, dict[str, int]] = {}
    for kind, field in _LIMIT_FIELD.items():
        out[kind] = {"used": await used(session, org_id, kind), "limit": int(getattr(plan, field))}  # type: ignore[arg-type]
    return out
