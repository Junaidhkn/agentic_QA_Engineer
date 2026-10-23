from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.discovery import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.tenancy.policy import require
from reliability.platform import idempotency
from reliability.platform.errors import AppError

router = APIRouter(tags=["discovery"])

MAX_OPENAPI_BYTES = 5 * 1024 * 1024


class DiscoveryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_ref: str | None = Field(default=None, max_length=200)
    openapi_spec_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    archive_artifact_id: uuid.UUID | None = None


class DiscoveryOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    commit_sha: str
    options: dict[str, Any]
    progress: list[dict[str, Any]]
    stats: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class OpenAPICreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["upload", "repo"] = "upload"
    source_ref: str | None = Field(default=None, max_length=300)
    document: str = Field(min_length=10, max_length=MAX_OPENAPI_BYTES)


class OpenAPIOut(BaseModel):
    id: uuid.UUID
    source: str
    source_ref: str | None
    operation_count: int
    created_at: datetime


class ArchiveOut(BaseModel):
    archive_artifact_id: uuid.UUID


def _out(r: service.DiscoveryRun) -> DiscoveryOut:
    return DiscoveryOut(
        id=r.id,
        project_id=r.project_id,
        status=r.status,
        commit_sha=r.commit_sha,
        options=r.options,
        progress=r.progress,
        stats=r.stats,
        error=r.error,
        created_at=r.created_at,
        finished_at=r.finished_at,
    )


@router.post("/projects/{project_id}/discoveries", status_code=202, response_model=DiscoveryOut)
async def start_discovery(
    project_id: uuid.UUID, body: DiscoveryCreate, ctx: CtxDep, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump(mode="json")
    if cached := await idempotency.lookup(ctx.session, project.org_id, key, payload):
        return cached.to_response()
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    run = await service.start(
        ctx.session,
        ctx.principal,
        project,
        commit_ref=body.commit_ref,
        openapi_spec_ids=body.openapi_spec_ids,
        archive_artifact_id=body.archive_artifact_id,
    )
    out = _out(run).model_dump(mode="json")
    await idempotency.store(ctx.session, project.org_id, key, payload, 202, out)
    return JSONResponse(out, status_code=202)


@router.get("/projects/{project_id}/discoveries", response_model=list[DiscoveryOut])
async def list_discoveries(project_id: uuid.UUID, ctx: CtxDep) -> list[DiscoveryOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return [_out(r) for r in await service.list_for(ctx.session, project_id)]


@router.get("/discoveries/{discovery_id}", response_model=DiscoveryOut)
async def get_discovery(discovery_id: uuid.UUID, ctx: CtxDep) -> DiscoveryOut:
    run = await service.get(ctx.session, discovery_id)
    require(ctx.principal, run.org_id, "viewer", project_id=run.project_id)
    return _out(run)


@router.post("/discoveries/{discovery_id}:cancel", status_code=202, response_model=DiscoveryOut)
async def cancel_discovery(discovery_id: uuid.UUID, ctx: CtxDep) -> Any:
    run = await service.get(ctx.session, discovery_id)
    require(ctx.principal, run.org_id, "engineer", project_id=run.project_id)
    await service.cancel(ctx.session, ctx.principal, run)
    return JSONResponse(_out(run).model_dump(mode="json"), status_code=202)


@router.post("/projects/{project_id}/openapi-specs", status_code=201, response_model=OpenAPIOut)
async def register_openapi(project_id: uuid.UUID, body: OpenAPICreate, ctx: CtxDep) -> OpenAPIOut:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    spec = await service.register_openapi(
        ctx.session, ctx.principal, project, body.document, body.source, body.source_ref
    )
    return OpenAPIOut(
        id=spec.id,
        source=spec.source,
        source_ref=spec.source_ref,
        operation_count=spec.operation_count,
        created_at=spec.created_at,
    )


@router.get("/projects/{project_id}/openapi-specs", response_model=list[OpenAPIOut])
async def list_openapi(project_id: uuid.UUID, ctx: CtxDep) -> list[OpenAPIOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return [
        OpenAPIOut(
            id=s.id,
            source=s.source,
            source_ref=s.source_ref,
            operation_count=s.operation_count,
            created_at=s.created_at,
        )
        for s in await service.list_openapi(ctx.session, project_id)
    ]


@router.post("/projects/{project_id}/source-archives", status_code=201, response_model=ArchiveOut)
async def upload_archive(project_id: uuid.UUID, request: Request, ctx: CtxDep) -> ArchiveOut:
    """Upload a .zip or .tar.gz of the source tree (request body) for discovery without GitHub."""
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    length = int(request.headers.get("content-length") or 0)
    if length > service.MAX_ARCHIVE_BYTES:
        raise AppError("VALIDATION_FAILED", "archive larger than 50 MB")
    data = await request.body()
    art_id = await service.upload_archive(ctx.session, ctx.principal, project, data)
    return ArchiveOut(archive_artifact_id=uuid.UUID(art_id))


@router.get("/projects/{project_id}/coverage")
async def coverage(project_id: uuid.UUID, ctx: CtxDep) -> dict[str, Any]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return await service.coverage(ctx.session, project_id)
