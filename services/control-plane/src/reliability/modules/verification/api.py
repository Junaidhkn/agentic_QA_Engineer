from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.runs.models import Run
from reliability.modules.verification import service
from reliability.platform import outbox
from reliability.platform.errors import AppError

router = APIRouter(tags=["verification"])


@router.get("/projects/{project_id}/noise-floor")
async def get_noise_floor(project_id: uuid.UUID, ctx: CtxDep) -> dict[str, Any]:
    project = await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return await service.latest_noise_floor(ctx.session, project.id)


@router.post("/projects/{project_id}/noise-floor", status_code=202)
async def measure_noise_floor(project_id: uuid.UUID, ctx: CtxDep) -> Any:
    """Start an A/A measurement (6 sandbox load runs) from the latest passed sandbox load run."""
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    candidates = (
        await ctx.session.execute(
            select(Run)
            .where(
                Run.project_id == project.id,
                Run.environment_kind == "sandbox_build",
                Run.kind == "load",
                Run.status == "passed",
            )
            .order_by(Run.created_at.desc())
            .limit(20)
        )
    ).scalars()
    template = next(
        (
            r
            for r in candidates
            if not any((r.config_snapshot or {}).get(k) for k in ("verification", "noise_floor", "experiment"))
        ),
        None,
    )
    if template is None:
        raise AppError("CONFLICT", "run a sandbox load test first; the A/A measurement reuses its build and workload")
    await outbox.enqueue(
        ctx.session,
        "verification.noise_floor_from_run",
        {"run_id": str(template.id), "org_id": str(project.org_id)},
        org_id=project.org_id,
    )
    return JSONResponse({"status": "started", "template_run_id": str(template.id)}, status_code=202)
