from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from reliability.modules.audit import service as audit
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.telemetry import ingest
from reliability.modules.telemetry.models import IngestToken
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

router = APIRouter(tags=["telemetry"])


class IngestTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


class IngestTokenOut(BaseModel):
    id: uuid.UUID
    name: str
    token_prefix: str
    created_at: datetime
    revoked_at: datetime | None


class IngestTokenCreated(IngestTokenOut):
    token: str
    otlp_endpoint: str
    env_example: dict[str, str]


@router.post("/projects/{project_id}/ingest-tokens", status_code=201, response_model=IngestTokenCreated)
async def create_token(project_id: uuid.UUID, body: IngestTokenIn, ctx: CtxDep) -> IngestTokenCreated:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    assert ctx.principal.user_id is not None
    row, token = await ingest.create(
        ctx.session, org_id=project.org_id, project_id=project.id, name=body.name, created_by=ctx.principal.user_id
    )
    await audit.record(
        ctx.session,
        ctx.actor,
        "ingest_token.created",
        org_id=project.org_id,
        subject_type="ingest_token",
        subject_id=row.id,
    )
    await ctx.session.refresh(row)
    endpoint = get_settings().otlp_public_url
    return IngestTokenCreated(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        created_at=row.created_at,
        revoked_at=None,
        token=token,
        otlp_endpoint=endpoint,
        env_example={
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {token}",
            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "0.1",
        },
    )


@router.get("/projects/{project_id}/ingest-tokens", response_model=list[IngestTokenOut])
async def list_tokens(project_id: uuid.UUID, ctx: CtxDep) -> list[IngestTokenOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "admin")
    rows = (
        await ctx.session.execute(
            select(IngestToken).where(IngestToken.project_id == project_id).order_by(IngestToken.created_at)
        )
    ).scalars()
    return [
        IngestTokenOut(
            id=r.id, name=r.name, token_prefix=r.token_prefix, created_at=r.created_at, revoked_at=r.revoked_at
        )
        for r in rows
    ]


@router.delete("/projects/{project_id}/ingest-tokens/{token_id}", status_code=204)
async def revoke_token(project_id: uuid.UUID, token_id: uuid.UUID, ctx: CtxDep) -> Response:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    if not await ingest.revoke(ctx.session, project_id, token_id):
        raise AppError("NOT_FOUND", "token not found")
    await audit.record(
        ctx.session,
        ctx.actor,
        "ingest_token.revoked",
        org_id=project.org_id,
        subject_type="ingest_token",
        subject_id=token_id,
    )
    return Response(status_code=204)
