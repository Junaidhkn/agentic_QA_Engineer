"""The single admission-control step for runs (FR-5, FR-14, FR-31, ADR-0024).

Checks run in a fixed order and fail with stable error codes:
role → organization state / kill switch → target verification → environment policy →
plan caps → concurrency → quota reservation → approval requirement.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.abuse import service as abuse
from reliability.modules.identity.service import Principal
from reliability.modules.legal import service as legal
from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.loadprofiles.service import check_plan
from reliability.modules.quotas import service as quotas
from reliability.modules.runs.models import ACTIVE_STATUSES, Run
from reliability.modules.targets import service as targets
from reliability.modules.targets.models import Target
from reliability.modules.tenancy.models import Organization
from reliability.modules.tenancy.policy import require
from reliability.platform import metrics
from reliability.platform.errors import AppError
from reliability.platform.flags import kill_switch_active

APPROVAL_VU_THRESHOLD = 500
LOAD_TYPES = ("baseline", "load", "stress", "spike", "soak")


@dataclass
class Admission:
    status: str  # "queued" | "pending_approval"
    approval_reasons: list[str] = field(default_factory=list)
    reservation: dict[str, int] = field(default_factory=dict)  # UsageKind -> quantity


@dataclass(frozen=True)
class RunRequest:
    org_id: uuid.UUID
    project_id: uuid.UUID
    kind: str  # functional | load | probe | verification
    target: Target | None
    profile: LoadProfile | None
    environment_kind: str  # target_url | sandbox_build
    run_id: uuid.UUID
    system: bool = False  # internal runs (verification) skip role checks and approvals


def vu_minutes(max_vus: int, duration_s: int) -> int:
    return max(1, max_vus * math.ceil(duration_s / 60))


async def _first_load_run(session: AsyncSession, target_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.target_id == target_id, Run.kind == "load", Run.status.in_(("passed", "failed", "invalid")))
    )
    return not count


async def admit(session: AsyncSession, principal: Principal | None, req: RunRequest) -> Admission:
    try:
        return await _admit(session, principal, req)
    except AppError as exc:
        metrics.ADMISSION_REJECTIONS.labels(exc.code).inc()
        raise


async def _admit(session: AsyncSession, principal: Principal | None, req: RunRequest) -> Admission:
    if not req.system:
        assert principal is not None
        require(principal, req.org_id, "engineer", scope="runs:write", project_id=req.project_id)

    org = await session.get(Organization, req.org_id)
    if org is None:
        raise AppError("NOT_FOUND", "organization not found")
    if org.suspended_at is not None:
        raise AppError("ORG_SUSPENDED", "organization is suspended", {"reason": org.suspended_reason})
    if await kill_switch_active(session, req.org_id):
        raise AppError("UNAVAILABLE", "test execution is paused by the operator", headers={"Retry-After": "600"})
    if not req.system:
        await legal.require_for_runs(session, req.org_id)  # Terms + AUP (load-testing consent)

    profile_type = req.profile.type if req.profile else "smoke"
    max_vus = req.profile.max_vus if req.profile else 1
    duration_s = req.profile.duration_s if req.profile else 600

    if req.environment_kind == "target_url":
        target = req.target
        if target is None:
            raise AppError("VALIDATION_FAILED", "target_id is required for target_url runs")
        if not target.is_runnable:
            if target.status == "verified":  # expired: re-check synchronously before refusing
                await targets.verify(session, principal, target)
            if not target.is_runnable:
                raise AppError(
                    "TARGET_NOT_VERIFIED",
                    f"target {target.host} is not verified",
                    {"target_id": str(target.id), "status": target.status},
                )
        violation = targets.policy_violation(target, profile_type, max_vus, duration_s)
        if violation:
            raise AppError("ENVIRONMENT_POLICY_VIOLATION", violation, {"environment": target.environment})

    if req.profile:
        await check_plan(session, req.org_id, profile_type, max_vus, duration_s)

    # Concurrency: per org (plan) and one active load run per target.
    plan = await quotas.get_plan(session, req.org_id)

    # Threat model A1: a new org's near-maximum first load test against a new host waits for review.
    if not req.system and req.target is not None and req.kind in abuse.LOAD_KINDS:
        await abuse.gate(
            session,
            abuse.Request(
                org_id=req.org_id,
                org_created_at=org.created_at,
                plan_max_vus=plan.max_vus,
                max_vus=max_vus,
                target=req.target,
                first_load_for_target=await _first_load_run(session, req.target.id),
            ),
        )
    active = await session.scalar(
        select(func.count()).select_from(Run).where(Run.org_id == req.org_id, Run.status.in_(ACTIVE_STATUSES))
    )
    if (active or 0) >= plan.max_concurrent_runs and not req.system:
        raise AppError(
            "CONCURRENCY_LIMIT", f"plan allows {plan.max_concurrent_runs} concurrent run(s)", {"active": active}
        )
    if req.target is not None and req.kind == "load":
        await session.execute(select(Target.id).where(Target.id == req.target.id).with_for_update())
        busy = await session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.target_id == req.target.id, Run.kind == "load", Run.status.in_(ACTIVE_STATUSES))
        )
        if busy:
            raise AppError("CONCURRENCY_LIMIT", "a load run is already active against this target")

    reservation: dict[quotas.UsageKind, int] = {}
    if req.kind in ("load", "verification") and req.profile:
        reservation["vu_minutes"] = vu_minutes(max_vus, duration_s)
    if req.environment_kind == "sandbox_build":
        reservation["sandbox_minutes"] = math.ceil(duration_s / 60) + 15
    for kind, qty in reservation.items():
        await quotas.reserve(session, req.org_id, kind, qty, req.run_id)

    reasons: list[str] = []
    if not req.system and req.target is not None:
        if req.target.environment == "production":
            reasons.append("production target")
        if profile_type in LOAD_TYPES and await _first_load_run(session, req.target.id):
            reasons.append("first load test against this target")
        if max_vus > APPROVAL_VU_THRESHOLD:
            reasons.append(f"more than {APPROVAL_VU_THRESHOLD} virtual users")
    return Admission("pending_approval" if reasons else "queued", reasons, {str(k): v for k, v in reservation.items()})
