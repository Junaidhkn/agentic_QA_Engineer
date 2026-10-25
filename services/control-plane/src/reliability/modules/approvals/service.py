"""Human approval gates (feature 8, FR-5, FR-27).

Subjects register a handler that runs, in the same transaction, when a decision is made.
Approvals carry a frozen ``summary`` of what is being approved (host, environment, VUs,
diff stats…) so the approver sees exactly what will happen.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.approvals.models import Approval
from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.tenancy.policy import require
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

Handler = Callable[[AsyncSession, Principal, Approval], Awaitable[None]]
_HANDLERS: dict[str, Handler] = {}
MIN_ROLE = {
    "run": "admin",
    "production_target": "owner",
    "workflow_destructive": "admin",
    "repair_pr": "engineer",
    "suite_pr": "engineer",
}
TTL = {
    "run": timedelta(hours=24),
    "repair_pr": timedelta(days=7),
    "suite_pr": timedelta(days=7),
    "production_target": timedelta(days=2),
    "workflow_destructive": timedelta(days=7),
}


def register_handler(subject_type: str, handler: Handler) -> None:
    _HANDLERS[subject_type] = handler


async def request(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    summary: dict[str, Any],
    requested_by: uuid.UUID | None,
) -> Approval:
    approval = Approval(
        id=uuid7(),
        org_id=org_id,
        subject_type=subject_type,
        subject_id=subject_id,
        summary=summary,
        requested_by=requested_by,
        expires_at=datetime.now(UTC) + TTL[subject_type],
    )
    session.add(approval)
    await session.flush()
    return approval


async def get(session: AsyncSession, approval_id: uuid.UUID) -> Approval:
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise AppError("NOT_FOUND", "approval not found")
    return approval


async def pending(session: AsyncSession, org_id: uuid.UUID) -> list[Approval]:
    return list(
        (
            await session.execute(
                select(Approval)
                .where(Approval.org_id == org_id, Approval.decision.is_(None), Approval.expires_at > datetime.now(UTC))
                .order_by(Approval.created_at)
            )
        ).scalars()
    )


async def for_subject(session: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> Approval | None:
    return (
        await session.execute(
            select(Approval)
            .where(Approval.subject_type == subject_type, Approval.subject_id == subject_id)
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def decide(
    session: AsyncSession, principal: Principal, approval: Approval, decision: str, reason: str | None
) -> Approval:
    require(principal, approval.org_id, MIN_ROLE[approval.subject_type])
    if approval.decision is not None:
        raise AppError("INVALID_STATE", "approval already decided")
    if approval.expires_at < datetime.now(UTC):
        raise AppError("INVALID_STATE", "approval request expired")
    if approval.subject_type == "production_target" and approval.requested_by == principal.user_id:
        raise AppError("FORBIDDEN", "production targets need a second person to approve (two-person rule)")
    approval.decision, approval.reason = decision, reason
    approval.decided_by, approval.decided_at = principal.user_id, datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        f"approval.{decision}",
        org_id=approval.org_id,
        subject_type=approval.subject_type,
        subject_id=approval.subject_id,
        metadata={"approval_id": str(approval.id), "reason": reason},
    )
    handler = _HANDLERS.get(approval.subject_type)
    if handler:
        await handler(session, principal, approval)
    return approval
