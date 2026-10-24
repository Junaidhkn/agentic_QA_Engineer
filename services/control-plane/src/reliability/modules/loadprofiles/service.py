"""Load profiles (FR-11, FR-16) with plan limits enforced at write time and again at admission."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.loadprofiles.compiler import template_stages, validate_profile
from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.projects.models import Project
from reliability.modules.quotas.service import get_plan
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7


async def check_plan(
    session: AsyncSession, org_id: uuid.UUID, profile_type: str, max_vus: int, duration_s: int
) -> None:
    plan = await get_plan(session, org_id)
    problems = []
    if profile_type not in plan.allowed_profile_types:
        problems.append(f"{profile_type} profiles are not included in the {plan.id} plan")
    if max_vus > plan.max_vus:
        problems.append(f"plan allows at most {plan.max_vus} VUs")
    if duration_s > plan.max_duration_s:
        problems.append(f"plan allows at most {plan.max_duration_s} s per run")
    if problems:
        raise AppError(
            "PLAN_LIMIT_EXCEEDED",
            "; ".join(problems),
            {
                "plan": plan.id,
                "max_vus": plan.max_vus,
                "max_duration_s": plan.max_duration_s,
                "allowed_profile_types": plan.allowed_profile_types,
            },
        )


async def create(session: AsyncSession, principal: Principal, project: Project, data: dict[str, Any]) -> LoadProfile:
    stages = data.get("stages") or template_stages(data["type"], data["max_vus"])
    duration_s = data.get("duration_s") or sum(s["duration_s"] for s in stages)
    validate_profile(data["type"], data["executor"], stages, data["max_vus"], duration_s)
    await check_plan(session, project.org_id, data["type"], data["max_vus"], duration_s)
    thresholds = {
        "p95_ms": project.default_slo["p95_ms"],
        "error_rate": project.default_slo["error_rate"],
        "abort_error_rate": 0.5,
        **(data.get("thresholds") or {}),
    }
    profile = LoadProfile(
        id=uuid7(),
        org_id=project.org_id,
        project_id=project.id,
        name=data["name"],
        type=data["type"],
        executor=data["executor"],
        stages=stages,
        max_vus=data["max_vus"],
        duration_s=duration_s,
        think_time_min_s=Decimal(str(data.get("think_time_min_s", 1))),
        think_time_max_s=Decimal(str(data.get("think_time_max_s", 3))),
        workload_mix=data.get("workload_mix") or {},
        thresholds=thresholds,
    )
    session.add(profile)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":  # unique (project_id, name)
            raise AppError("CONFLICT", "a profile with this name already exists") from exc
        raise AppError(
            "VALIDATION_FAILED", "profile values out of range (duration 10–3600 s, 1–1000 VUs, think time min ≤ max)"
        ) from exc
    await audit.record(
        session,
        principal.actor,
        "load_profile.created",
        org_id=project.org_id,
        subject_type="load_profile",
        subject_id=profile.id,
        metadata={"type": profile.type, "max_vus": profile.max_vus, "duration_s": duration_s},
    )
    return profile


async def update(
    session: AsyncSession, principal: Principal, profile: LoadProfile, data: dict[str, Any]
) -> LoadProfile:
    for field in ("name", "executor", "stages", "max_vus", "workload_mix"):
        if data.get(field) is not None:
            setattr(profile, field, data[field])
    if data.get("thresholds"):
        profile.thresholds = {**profile.thresholds, **data["thresholds"]}
    for field in ("think_time_min_s", "think_time_max_s"):
        if data.get(field) is not None:
            setattr(profile, field, Decimal(str(data[field])))
    if data.get("stages") is not None:
        profile.duration_s = sum(s["duration_s"] for s in profile.stages)
    validate_profile(profile.type, profile.executor, profile.stages, profile.max_vus, profile.duration_s)
    await check_plan(session, profile.org_id, profile.type, profile.max_vus, profile.duration_s)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "load_profile.updated",
        org_id=profile.org_id,
        subject_type="load_profile",
        subject_id=profile.id,
    )
    return profile


async def get(session: AsyncSession, profile_id: uuid.UUID) -> LoadProfile:
    profile = await session.get(LoadProfile, profile_id)
    if profile is None:
        raise AppError("NOT_FOUND", "load profile not found")
    return profile


async def list_for(session: AsyncSession, project_id: uuid.UUID) -> list[LoadProfile]:
    return list(
        (
            await session.execute(
                select(LoadProfile).where(LoadProfile.project_id == project_id).order_by(LoadProfile.created_at)
            )
        ).scalars()
    )
