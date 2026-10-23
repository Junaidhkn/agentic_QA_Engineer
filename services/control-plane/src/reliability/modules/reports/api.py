from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.reports import service
from reliability.platform.errors import AppError

router = APIRouter(tags=["reports"])


@router.get("/projects/{project_id}/summary")
async def summary(project_id: uuid.UUID, ctx: CtxDep) -> dict[str, Any]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return await service.project_summary(ctx.session, project_id)


@router.get("/projects/{project_id}/trends")
async def trends(
    project_id: uuid.UUID, ctx: CtxDep, scope: str = "overall", metric: str = "p95_ms", limit: int = 30
) -> list[dict[str, Any]]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    try:
        return await service.trends(ctx.session, project_id, scope, metric, max(1, min(limit, 200)))
    except ValueError as exc:
        raise AppError("VALIDATION_FAILED", str(exc), {"metrics": list(service.TREND_METRICS)}) from exc
