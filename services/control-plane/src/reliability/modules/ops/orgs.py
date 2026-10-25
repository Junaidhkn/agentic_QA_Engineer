"""Operator actions on organizations (runbooks: abuse report, customer data deletion).

* ``suspend`` stops an org at once: admission refuses its runs (``ORG_SUSPENDED``), queued
  runs error at lease time, and active leases are told to cancel at the next heartbeat.
* ``delete`` erases an org's data (DPA deletion request): objects under ``org/<id>/`` in the
  object store, LangGraph checkpoints of its agent threads, every row carrying its ``org_id``
  and users whose only membership it was. The org's audit events are *kept*: the audit log is
  append-only for every runtime role (ADR-0023) and is retained for its retention period as
  the record of what was done (stated in the DPA). One ``org.deleted`` event with row counts —
  no names or emails — is added to the platform chain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.runners.service import request_cancel_all
from reliability.modules.tenancy.models import Organization
from reliability.platform.actor import Actor
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.storage import get_store

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
# Deleted last: rows other tables reference.
LAST = ("memberships", "organizations")
RETAINED = ("audit_events",)  # append-only by design (ADR-0023)


@dataclass
class DeletionPlan:
    org_id: uuid.UUID
    slug: str
    rows: dict[str, int] = field(default_factory=dict)
    agent_threads: int = 0
    users_to_delete: int = 0
    object_prefix: str = ""
    retained: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "org_id": str(self.org_id),
            "slug": self.slug,
            "rows": {k: v for k, v in sorted(self.rows.items()) if v},
            "agent_threads": self.agent_threads,
            "users_to_delete": self.users_to_delete,
            "object_prefix": self.object_prefix,
            "retained": self.retained,
        }


async def _org(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise AppError("NOT_FOUND", "organization not found")
    return org


async def suspend(org_id: uuid.UUID, reason: str, operator: str) -> int:
    async with system_tx() as s:
        org = await _org(s, org_id)
        org.suspended_at, org.suspended_reason = datetime.now(UTC), reason[:500]
        await audit.record(
            s,
            Actor("operator", operator),
            "org.suspended",
            org_id=org_id,
            subject_type="org",
            subject_id=org_id,
            metadata={"reason": reason[:500]},
        )
    return await request_cancel_all(org_id)


async def unsuspend(org_id: uuid.UUID, operator: str) -> None:
    async with system_tx() as s:
        org = await _org(s, org_id)
        org.suspended_at, org.suspended_reason = None, None
        await audit.record(
            s, Actor("operator", operator), "org.unsuspended", org_id=org_id, subject_type="org", subject_id=org_id
        )


async def change_plan(org_id: uuid.UUID, plan_id: str, operator: str) -> dict[str, Any]:
    """Move an org to another plan (manual billing until self-serve checkout exists). Limits
    apply to new work immediately; existing projects above a lower limit stay but no new ones
    can be created — reported as ``over_limits``."""
    async with system_tx() as s:
        org = await _org(s, org_id)
        plan = (await s.execute(text("SELECT id, max_projects FROM plans WHERE id = :p"), {"p": plan_id})).first()
        if plan is None:
            raise AppError("NOT_FOUND", f"plan {plan_id} not found")
        previous = org.plan_id
        org.plan_id = plan_id
        projects = int(await s.scalar(text("SELECT count(*) FROM projects WHERE org_id = :o"), {"o": org_id}) or 0)
        await audit.record(
            s,
            Actor("operator", operator),
            "org.plan_changed",
            org_id=org_id,
            subject_type="org",
            subject_id=org_id,
            metadata={"from": previous, "to": plan_id},
        )
    over = {"projects": [projects, plan.max_projects]} if projects > plan.max_projects else {}
    return {"org_id": str(org_id), "from": previous, "to": plan_id, "over_limits": over}


async def _org_tables(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        text(
            """
            SELECT c.table_name FROM information_schema.columns c
            JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public' AND c.column_name = 'org_id' AND t.table_type = 'BASE TABLE'
              AND c.table_name NOT IN (SELECT inhrelid::regclass::text FROM pg_inherits)
            ORDER BY c.table_name
            """
        )
    )
    return [r[0] for r in rows]


async def _sole_members(session: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        text(
            "SELECT m.user_id FROM memberships m WHERE m.org_id = :o AND NOT EXISTS "
            "(SELECT 1 FROM memberships x WHERE x.user_id = m.user_id AND x.org_id <> :o)"
        ),
        {"o": org_id},
    )
    return [r[0] for r in rows]


async def deletion_plan(org_id: uuid.UUID) -> DeletionPlan:
    async with system_tx() as s:
        org = await _org(s, org_id)
        plan = DeletionPlan(org_id, org.slug, object_prefix=f"org/{org_id}/")
        for table in await _org_tables(s):
            plan.rows[table] = int(
                await s.scalar(text(f'SELECT count(*) FROM "{table}" WHERE org_id = :o'), {"o": org_id}) or 0  # noqa: S608
            )
        plan.rows["organizations"] = 1
        plan.retained = {t: plan.rows.pop(t) for t in RETAINED if t in plan.rows}
        plan.agent_threads = plan.rows.get("agent_threads", 0)
        plan.users_to_delete = len(await _sole_members(s, org_id))
    return plan


async def delete(org_id: uuid.UUID, confirm_slug: str, operator: str) -> dict[str, Any]:
    """Irreversible. ``confirm_slug`` must equal the org's slug."""
    plan = await deletion_plan(org_id)
    if confirm_slug != plan.slug:
        raise AppError("VALIDATION_FAILED", "confirmation slug does not match the organization")
    await suspend(org_id, "deletion in progress", operator)
    objects = get_store().delete_prefix(plan.object_prefix)
    deleted: dict[str, int] = {}
    async with system_tx() as s:
        threads = [
            r[0] for r in await s.execute(text("SELECT thread_id FROM agent_threads WHERE org_id = :o"), {"o": org_id})
        ]
        for table in CHECKPOINT_TABLES:
            if threads and await s.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"agent_state.{table}"}):
                res = await s.execute(
                    text(f"DELETE FROM agent_state.{table} WHERE thread_id = ANY(:t)"),  # noqa: S608
                    {"t": threads},
                )
                deleted[f"agent_state.{table}"] = int(res.rowcount)  # type: ignore[attr-defined]
        users = await _sole_members(s, org_id)
        tables = [t for t in await _org_tables(s) if t not in LAST + RETAINED]
        # FK order varies; retry tables blocked by a reference until a pass makes no progress.
        pending = list(tables)
        while pending:
            blocked = []
            for table in pending:
                try:
                    async with s.begin_nested():
                        res = await s.execute(text(f'DELETE FROM "{table}" WHERE org_id = :o'), {"o": org_id})  # noqa: S608
                    deleted[table] = deleted.get(table, 0) + int(res.rowcount)  # type: ignore[attr-defined]
                except Exception:
                    blocked.append(table)
            if len(blocked) == len(pending):
                raise AppError("CONFLICT", f"could not delete rows in {blocked}")
            pending = blocked
        for table in LAST:
            column = "id" if table == "organizations" else "org_id"
            res = await s.execute(text(f"DELETE FROM {table} WHERE {column} = :o"), {"o": org_id})  # noqa: S608
            deleted[table] = int(res.rowcount)  # type: ignore[attr-defined]
        if users:
            res = await s.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": users})
            deleted["users"] = int(res.rowcount)  # type: ignore[attr-defined]
        summary = {
            "rows": {k: v for k, v in sorted(deleted.items()) if v},
            "objects": objects,
            "retained": {
                t: int(await s.scalar(text(f"SELECT count(*) FROM {t} WHERE org_id = :o"), {"o": org_id}) or 0)  # noqa: S608
                for t in RETAINED
            },
        }
        await audit.record(
            s,
            Actor("operator", operator),
            "org.deleted",
            org_id=None,
            subject_type="org",
            subject_id=org_id,
            metadata=summary,
        )
    return {"org_id": str(org_id), **summary}
