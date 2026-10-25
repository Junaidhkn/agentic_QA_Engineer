from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.identity.deps import CtxDep
from reliability.modules.loadprofiles import service
from reliability.modules.loadprofiles.compiler import TEMPLATES, template_stages
from reliability.modules.projects import service as projects
from reliability.modules.tenancy.policy import require

router = APIRouter(tags=["load-profiles"])
ProfileType = Literal["smoke", "baseline", "load", "stress", "spike", "soak"]
Executor = Literal["ramping-vus", "ramping-arrival-rate", "constant-arrival-rate"]


class StageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_s: int = Field(ge=1, le=3600)
    target: int = Field(ge=0, le=100000)


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p95_ms: int | None = Field(default=None, ge=1, le=60000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    abort_error_rate: float | None = Field(default=None, gt=0, le=1)


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    type: ProfileType
    executor: Executor = "ramping-vus"
    max_vus: int = Field(ge=1, le=1000)
    stages: list[StageIn] | None = Field(default=None, description="Omit to use the template for the type")
    think_time_min_s: float = Field(default=1, ge=0, le=60)
    think_time_max_s: float = Field(default=3, ge=0, le=60)
    workload_mix: dict[str, float] = Field(default_factory=dict, description="workflow key -> weight")
    thresholds: Thresholds | None = None


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    executor: Executor | None = None
    max_vus: int | None = Field(default=None, ge=1, le=1000)
    stages: list[StageIn] | None = None
    think_time_min_s: float | None = None
    think_time_max_s: float | None = None
    workload_mix: dict[str, float] | None = None
    thresholds: Thresholds | None = None


class ProfileOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    type: str
    executor: str
    max_vus: int
    duration_s: int
    stages: list[StageIn]
    think_time_min_s: float
    think_time_max_s: float
    workload_mix: dict[str, float]
    thresholds: dict[str, float]
    updated_at: datetime


def _out(p: service.LoadProfile) -> ProfileOut:
    return ProfileOut(
        id=p.id,
        project_id=p.project_id,
        name=p.name,
        type=p.type,
        executor=p.executor,
        max_vus=p.max_vus,
        duration_s=p.duration_s,
        stages=[StageIn(**s) for s in p.stages],
        think_time_min_s=float(p.think_time_min_s),
        think_time_max_s=float(p.think_time_max_s),
        workload_mix=p.workload_mix,
        thresholds=p.thresholds,
        updated_at=p.updated_at,
    )


@router.get("/load-profile-templates")
async def templates(max_vus: int = 1000) -> dict[str, list[dict[str, int]]]:
    return {t: template_stages(t, max(1, min(max_vus, 1000))) for t in TEMPLATES}


@router.get("/projects/{project_id}/load-profiles", response_model=list[ProfileOut])
async def list_profiles(project_id: uuid.UUID, ctx: CtxDep) -> list[ProfileOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return [_out(p) for p in await service.list_for(ctx.session, project_id)]


@router.post("/projects/{project_id}/load-profiles", status_code=201, response_model=ProfileOut)
async def create_profile(project_id: uuid.UUID, body: ProfileIn, ctx: CtxDep) -> ProfileOut:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    data = body.model_dump()
    data["thresholds"] = body.thresholds.model_dump(exclude_none=True) if body.thresholds else None
    return _out(await service.create(ctx.session, ctx.principal, project, data))


@router.get("/load-profiles/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: uuid.UUID, ctx: CtxDep) -> ProfileOut:
    p = await service.get(ctx.session, profile_id)
    require(ctx.principal, p.org_id, "viewer", scope="runs:read", project_id=p.project_id)
    return _out(p)


@router.patch("/load-profiles/{profile_id}", response_model=ProfileOut)
async def patch_profile(profile_id: uuid.UUID, body: ProfilePatch, ctx: CtxDep) -> ProfileOut:
    p = await service.get(ctx.session, profile_id)
    require(ctx.principal, p.org_id, "engineer")
    data = body.model_dump(exclude_none=True)
    if body.thresholds:
        data["thresholds"] = body.thresholds.model_dump(exclude_none=True)
    return _out(await service.update(ctx.session, ctx.principal, p, data))


@router.delete("/load-profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: uuid.UUID, ctx: CtxDep) -> Response:
    p = await service.get(ctx.session, profile_id)
    require(ctx.principal, p.org_id, "engineer")
    await ctx.session.delete(p)
    return Response(status_code=204)
