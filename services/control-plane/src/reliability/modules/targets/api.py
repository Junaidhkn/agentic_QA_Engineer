from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.targets import service
from reliability.modules.tenancy.policy import require
from reliability.platform.settings import get_settings

router = APIRouter(tags=["targets"])


class TargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=8, max_length=500)
    environment: Literal["sandbox", "preview", "staging", "production"]


class TargetOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    base_url: str
    host: str
    environment: str
    status: str
    verification_method: str | None
    verified_at: datetime | None
    verification_expires_at: datetime | None
    last_checked_at: datetime | None
    last_check_detail: dict[str, Any] | None
    verification: dict[str, Any]
    egress_ips: list[str]
    environment_policy: dict[str, Any]


def _out(t: service.Target) -> TargetOut:
    policy = dict(service.ENVIRONMENT_POLICY[t.environment])
    policy["profiles"] = sorted(policy["profiles"])
    return TargetOut(
        id=t.id,
        project_id=t.project_id,
        base_url=t.base_url,
        host=t.host,
        environment=t.environment,
        status=t.status if t.status != "verified" or t.is_runnable else "expired",
        verification_method=t.verification_method,
        verified_at=t.verified_at,
        verification_expires_at=t.verification_expires_at,
        last_checked_at=t.last_checked_at,
        last_check_detail=t.last_check_detail,
        verification=service.instructions(t),
        egress_ips=get_settings().egress_ips,
        environment_policy=policy,
    )


@router.post("/projects/{project_id}/targets", status_code=201, response_model=TargetOut)
async def register(project_id: uuid.UUID, body: TargetCreate, ctx: CtxDep) -> TargetOut:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    target = await service.register(
        ctx.session,
        ctx.principal,
        org_id=project.org_id,
        project_id=project.id,
        base_url=body.base_url,
        environment=body.environment,
    )
    await ctx.session.refresh(target)
    return _out(target)


@router.get("/projects/{project_id}/targets", response_model=list[TargetOut])
async def list_targets(project_id: uuid.UUID, ctx: CtxDep) -> list[TargetOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="projects:read")
    return [_out(t) for t in await service.list_for_project(ctx.session, project_id)]


@router.get("/targets/{target_id}", response_model=TargetOut)
async def get_target(target_id: uuid.UUID, ctx: CtxDep) -> TargetOut:
    target = await service.get(ctx.session, target_id)
    require(ctx.principal, target.org_id, "viewer", scope="projects:read", project_id=target.project_id)
    return _out(target)


@router.post("/targets/{target_id}:verify", response_model=TargetOut)
async def verify_target(target_id: uuid.UUID, ctx: CtxDep) -> TargetOut:
    target = await service.get(ctx.session, target_id)
    require(ctx.principal, target.org_id, "admin")
    await ratelimit.check(str(target_id), "verify")
    target = await service.verify(ctx.session, ctx.principal, target)
    return _out(target)


@router.delete("/targets/{target_id}", status_code=204)
async def revoke_target(target_id: uuid.UUID, ctx: CtxDep) -> Response:
    target = await service.get(ctx.session, target_id)
    require(ctx.principal, target.org_id, "admin")
    await service.revoke(ctx.session, ctx.principal, target)
    return Response(status_code=204)
