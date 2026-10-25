from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from reliability.modules.artifacts import service as artifacts
from reliability.modules.artifacts.api import DownloadUrl
from reliability.modules.environments import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.tenancy.policy import require
from reliability.platform.errors import AppError

router = APIRouter(tags=["sandboxes"])


class BuildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_ref: str | None = Field(default=None, max_length=200)
    archive_artifact_id: uuid.UUID | None = None


class BuildOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    source: dict[str, Any]
    commit_sha: str | None
    plan: dict[str, Any] | None
    report: dict[str, Any] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def _out(b: service.SandboxBuild) -> BuildOut:
    return BuildOut(
        id=b.id,
        project_id=b.project_id,
        status=b.status,
        source=b.source,
        commit_sha=b.commit_sha,
        plan=b.plan,
        report=b.report,
        error=b.error,
        created_at=b.created_at,
        finished_at=b.finished_at,
    )


@router.post("/projects/{project_id}/sandbox-builds", status_code=202, response_model=BuildOut)
async def start_build(project_id: uuid.UUID, body: BuildCreate, ctx: CtxDep) -> Any:
    """Check that the app builds and serves in a sandbox (plan → build → start → migrate/seed → health)."""
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    build = await service.start_build(
        ctx.session, ctx.principal, project, commit_ref=body.commit_ref, archive_artifact_id=body.archive_artifact_id
    )
    return JSONResponse(_out(build).model_dump(mode="json"), status_code=202)


@router.get("/projects/{project_id}/sandbox-builds", response_model=list[BuildOut])
async def list_builds(project_id: uuid.UUID, ctx: CtxDep) -> list[BuildOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return [_out(b) for b in await service.list_for(ctx.session, project_id)]


@router.get("/sandbox-builds/{build_id}", response_model=BuildOut)
async def get_build(build_id: uuid.UUID, ctx: CtxDep) -> BuildOut:
    build = await service.get(ctx.session, build_id)
    require(ctx.principal, build.org_id, "viewer", project_id=build.project_id)
    return _out(build)


@router.get("/sandbox-builds/{build_id}/log", response_model=DownloadUrl)
async def build_log(build_id: uuid.UUID, ctx: CtxDep) -> DownloadUrl:
    build = await service.get(ctx.session, build_id)
    require(ctx.principal, build.org_id, "engineer", project_id=build.project_id)
    key = artifacts.new_key(build.org_id, build.project_id, "log_bundle", build.id, "sandbox.log")
    art = (
        await ctx.session.execute(select(artifacts.Artifact).where(artifacts.Artifact.storage_key == key))
    ).scalar_one_or_none()
    if art is None:
        raise AppError("NOT_FOUND", "no build log yet")
    return DownloadUrl(url=artifacts.download_url(art, 300), expires_in_s=300)
