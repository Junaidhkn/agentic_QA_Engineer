from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.secrets import service

router = APIRouter(tags=["secrets"])


class SecretIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=8192)


class SecretMeta(BaseModel):
    name: str
    created_at: datetime
    rotated_at: datetime | None


@router.put("/projects/{project_id}/secrets/{name}", status_code=204)
async def put_secret(project_id: uuid.UUID, name: str, body: SecretIn, ctx: CtxDep) -> Response:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    assert ctx.principal.user_id is not None
    await service.put(
        ctx.session,
        ctx.actor,
        org_id=project.org_id,
        project_id=project.id,
        name=name,
        value=body.value,
        user_id=ctx.principal.user_id,
    )
    return Response(status_code=204)


@router.get("/projects/{project_id}/secrets", response_model=list[SecretMeta])
async def list_secrets(project_id: uuid.UUID, ctx: CtxDep) -> list[SecretMeta]:
    await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    return [
        SecretMeta(name=s.name, created_at=s.created_at, rotated_at=s.rotated_at)
        for s in await service.list_names(ctx.session, project_id)
    ]


@router.delete("/projects/{project_id}/secrets/{name}", status_code=204)
async def delete_secret(project_id: uuid.UUID, name: str, ctx: CtxDep) -> Response:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    await service.remove(ctx.session, ctx.actor, project.org_id, project.id, name)
    return Response(status_code=204)
