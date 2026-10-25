from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from reliability.modules.quotas.models import Plan
from reliability.platform.db import tenant_tx

router = APIRouter(tags=["plans"])


class PlanOut(BaseModel):
    id: str
    name: str
    price_usd_month: int | None
    max_vus: int
    max_duration_s: int
    allowed_profile_types: list[str]
    monthly_vu_minutes: int
    monthly_sandbox_minutes: int
    monthly_llm_tokens: int
    max_concurrent_runs: int
    max_projects: int
    artifact_retention_days: int
    features: dict[str, Any]


@router.get("/plans", response_model=list[PlanOut])
async def plans() -> list[PlanOut]:
    """Public: plans and their limits (pricing page). ``price_usd_month`` null = contact sales."""
    async with tenant_tx() as s:
        rows = (await s.execute(select(Plan).order_by(Plan.sort_order))).scalars()
        return [
            PlanOut(
                id=p.id,
                name=p.display_name,
                price_usd_month=p.price_usd_month,
                max_vus=p.max_vus,
                max_duration_s=p.max_duration_s,
                allowed_profile_types=list(p.allowed_profile_types),
                monthly_vu_minutes=p.monthly_vu_minutes,
                monthly_sandbox_minutes=p.monthly_sandbox_minutes,
                monthly_llm_tokens=p.monthly_llm_tokens,
                max_concurrent_runs=p.max_concurrent_runs,
                max_projects=p.max_projects,
                artifact_retention_days=p.artifact_retention_days,
                features=p.features,
            )
            for p in rows
        ]
