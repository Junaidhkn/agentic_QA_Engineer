from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.audit import service as audit
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service
from reliability.modules.tenancy.policy import default_org

router = APIRouter(tags=["projects"])


class Slo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p95_ms: int = Field(default=500, ge=1, le=60000)
    error_rate: float = Field(default=0.01, ge=0, le=1)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    default_slo: Slo | None = None


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    default_slo: Slo | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    default_slo: Slo
    created_at: datetime


def _out(p: service.Project) -> ProjectOut:
    return ProjectOut(id=p.id, org_id=p.org_id, name=p.name, default_slo=Slo(**p.default_slo), created_at=p.created_at)


@router.post("/projects", status_code=201, response_model=ProjectOut)
async def create_project(
    body: ProjectCreate, ctx: CtxDep, x_org_id: Annotated[str | None, Header()] = None
) -> ProjectOut:
    org_id = default_org(ctx.principal, x_org_id)
    slo = body.default_slo.model_dump() if body.default_slo else None
    return _out(await service.create(ctx.session, ctx.principal, org_id, body.name, slo))


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(ctx: CtxDep, x_org_id: Annotated[str | None, Header()] = None) -> list[ProjectOut]:
    org_id = default_org(ctx.principal, x_org_id) if x_org_id else None
    return [_out(p) for p in await service.list_for(ctx.session, ctx.principal, org_id)]


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, ctx: CtxDep) -> ProjectOut:
    return _out(await service.load(ctx.session, ctx.principal, project_id, "viewer", scope="projects:read"))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def patch_project(project_id: uuid.UUID, body: ProjectPatch, ctx: CtxDep) -> ProjectOut:
    project = await service.load(ctx.session, ctx.principal, project_id, "admin")
    if body.name is not None:
        project.name = body.name
    if body.default_slo is not None:
        project.default_slo = body.default_slo.model_dump()
    await audit.record(
        ctx.session,
        ctx.actor,
        "project.updated",
        org_id=project.org_id,
        subject_type="project",
        subject_id=project.id,
        metadata=body.model_dump(exclude_none=True),
    )
    await ctx.session.flush()
    return _out(project)


@router.delete("/projects/{project_id}", status_code=204)
async def archive_project(project_id: uuid.UUID, ctx: CtxDep) -> Response:
    project = await service.load(ctx.session, ctx.principal, project_id, "owner")
    project.archived_at = datetime.now(UTC)
    await audit.record(
        ctx.session, ctx.actor, "project.archived", org_id=project.org_id, subject_type="project", subject_id=project.id
    )
    return Response(status_code=204)
