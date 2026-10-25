"""Abuse review queue (threat model A1, ADR-0010/0024).

Ownership verification stops most misuse; this catches the pattern verification cannot:
a brand-new organization asking for a near-maximum load test against a host it has never
load-tested — or hunting for verifiable hosts (many new targets, many failed verifications).
Such load runs are refused with ``UNDER_REVIEW`` and the org lands in the review queue; org
owners are emailed, an operator clears or suspends (``reliability abuse …``), and a cleared
org is not queued again. Runs against sandboxes (our own copy of the app) and system runs
are never held.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.abuse.models import AbuseReview
from reliability.modules.audit import service as audit
from reliability.modules.targets.models import Target
from reliability.platform import outbox
from reliability.platform.actor import Actor
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

NEW_ORG = timedelta(days=7)
VU_SHARE = 0.8  # of the plan's maximum
MIN_VUS = 50
CHURN_WINDOW = timedelta(hours=24)
CHURN_TARGETS = 5
CHURN_FAILED = 3
LOAD_KINDS = {"load"}


@dataclass(frozen=True)
class Request:
    org_id: uuid.UUID
    org_created_at: datetime
    plan_max_vus: int
    max_vus: int
    target: Target
    first_load_for_target: bool


async def _churn(session: AsyncSession, org_id: uuid.UUID, now: datetime) -> tuple[int, int]:
    since = now - CHURN_WINDOW
    created = await session.scalar(
        select(func.count()).select_from(Target).where(Target.org_id == org_id, Target.created_at >= since)
    )
    failed = await session.scalar(
        select(func.count())
        .select_from(Target)
        .where(Target.org_id == org_id, Target.created_at >= since, Target.status == "failed")
    )
    return int(created or 0), int(failed or 0)


async def signals(session: AsyncSession, req: Request, now: datetime | None = None) -> dict[str, Any] | None:
    """The rule that fired and its evidence, or None. Read-only (the caller's tenant session)."""
    now = now or datetime.now(UTC)
    org_age = now - req.org_created_at
    created, failed = await _churn(session, req.org_id, now)
    evidence = {
        "org_age_h": round(org_age.total_seconds() / 3600, 1),
        "max_vus": req.max_vus,
        "plan_max_vus": req.plan_max_vus,
        "target_host": req.target.host,
        "target_environment": req.target.environment,
        "first_load_for_target": req.first_load_for_target,
        "targets_created_24h": created,
        "failed_verifications_24h": failed,
    }
    big = req.max_vus >= max(MIN_VUS, int(req.plan_max_vus * VU_SHARE))
    if org_age < NEW_ORG and big and req.first_load_for_target:
        return {"rule": "new_org_max_load_new_target", **evidence}
    if created >= CHURN_TARGETS and failed >= CHURN_FAILED:
        return {"rule": "verification_churn", **evidence}
    return None


async def _status(org_id: uuid.UUID) -> AbuseReview | None:
    """The org's most relevant review: an open one, else the latest decision."""
    async with system_tx() as s:
        rows = list(
            (
                await s.execute(
                    select(AbuseReview).where(AbuseReview.org_id == org_id).order_by(AbuseReview.created_at.desc())
                )
            ).scalars()
        )
    return next((r for r in rows if r.status == "open"), rows[0] if rows else None)


async def gate(session: AsyncSession, req: Request) -> None:
    """Called by admission for load runs against customer targets; raises ``UNDER_REVIEW``."""
    found = await signals(session, req)
    if found is None:
        return  # only runs that trip a rule wait; smaller tests keep working during a review
    current = await _status(req.org_id)
    if current is not None and current.status == "cleared":
        return  # an operator vouched for this org
    if current is not None and current.status == "open":
        raise _under_review(current.id)
    # The request transaction rolls back with the refusal; the review is committed on its own.
    review_id = uuid7()
    async with system_tx() as s:
        s.add(AbuseReview(id=review_id, org_id=req.org_id, reason=found["rule"], signals=found, status="open"))
        await audit.record(
            s,
            Actor("system", "abuse"),
            "abuse.review_opened",
            org_id=req.org_id,
            subject_type="abuse_review",
            subject_id=review_id,
            metadata={"rule": found["rule"]},
        )
        await outbox.enqueue(s, "abuse.notify", {"review_id": str(review_id), "event": "opened"}, org_id=req.org_id)
    raise _under_review(review_id)


def _under_review(review_id: uuid.UUID) -> AppError:
    return AppError(
        "UNDER_REVIEW",
        "this load test is waiting for a short safety review of your organization (usually within one business "
        "day); org owners will get an email when it is done. Smaller tests and sandbox runs are not affected.",
        {"review_id": str(review_id)},
    )


# --- operator side ------------------------------------------------------------------------
async def list_reviews(status: str | None = "open") -> list[AbuseReview]:
    async with system_tx() as s:
        stmt = select(AbuseReview).order_by(AbuseReview.created_at)
        if status:
            stmt = stmt.where(AbuseReview.status == status)
        return list((await s.execute(stmt)).scalars())


async def get(review_id: uuid.UUID) -> AbuseReview:
    async with system_tx() as s:
        review = await s.get(AbuseReview, review_id)
    if review is None:
        raise AppError("NOT_FOUND", "abuse review not found")
    return review


async def decide(review_id: uuid.UUID, decision: str, operator: str, note: str) -> AbuseReview:
    """``cleared`` lets the org run again; ``suspended`` suspends the whole org."""
    if decision not in ("cleared", "suspended"):
        raise AppError("VALIDATION_FAILED", "decision must be cleared or suspended")
    async with system_tx() as s:
        review = await s.get(AbuseReview, review_id, with_for_update=True)
        if review is None:
            raise AppError("NOT_FOUND", "abuse review not found")
        if review.status != "open":
            raise AppError("INVALID_STATE", f"review already {review.status}")
        review.status, review.decided_by, review.decided_at = decision, operator, datetime.now(UTC)
        review.signals = {**review.signals, "decision_note": note[:1000]}
        await audit.record(
            s,
            Actor("operator", operator),
            f"abuse.review_{decision}",
            org_id=review.org_id,
            subject_type="abuse_review",
            subject_id=review.id,
            metadata={"note": note[:1000]},
        )
        await outbox.enqueue(s, "abuse.notify", {"review_id": str(review.id), "event": decision}, org_id=review.org_id)
        org_id = review.org_id
    if decision == "suspended":
        from reliability.modules.ops import orgs

        await orgs.suspend(org_id, f"abuse review {review_id}: {note}"[:500], operator)
    return await get(review_id)


async def owner_emails(org_id: uuid.UUID) -> list[str]:
    async with system_tx() as s:
        rows = await s.execute(
            text(
                "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.org_id = :o AND m.role = 'owner' AND u.disabled_at IS NULL"
            ),
            {"o": org_id},
        )
        return [str(r[0]) for r in rows]
