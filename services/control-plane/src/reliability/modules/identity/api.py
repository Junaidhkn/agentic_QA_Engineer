from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.audit import service as audit
from reliability.modules.identity import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.tenancy.policy import require
from reliability.platform.errors import AppError

router = APIRouter(tags=["identity"])


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    role: str


class MeOut(BaseModel):
    kind: str
    user_id: uuid.UUID | None
    email: str | None
    mfa: bool
    memberships: list[MembershipOut]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1)
    project_ids: list[uuid.UUID] | None = None
    expires_in_days: int = Field(default=90, ge=1, le=365)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    project_ids: list[uuid.UUID] | None
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    secret: str = Field(description="Shown once. Store it securely.")


def _key_out(k: service.ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        key_prefix=k.key_prefix,
        scopes=k.scopes,
        project_ids=k.project_ids,
        expires_at=k.expires_at,
        last_used_at=k.last_used_at,
        revoked_at=k.revoked_at,
        created_at=k.created_at,
    )


@router.get("/me", response_model=MeOut)
async def me(ctx: CtxDep) -> MeOut:
    p = ctx.principal
    return MeOut(
        kind=p.kind,
        user_id=p.user_id,
        email=p.email,
        mfa=p.mfa,
        memberships=[MembershipOut(org_id=o, role=r) for o, r in p.org_roles.items()],
    )


@router.post("/orgs/{org_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
async def create_key(org_id: uuid.UUID, body: ApiKeyCreate, ctx: CtxDep) -> ApiKeyCreated:
    require(ctx.principal, org_id, "admin")
    if ctx.principal.user_id is None:
        raise AppError("FORBIDDEN", "only users can create API keys")
    created = await service.create_api_key(
        ctx.session,
        org_id=org_id,
        created_by=ctx.principal.user_id,
        name=body.name,
        scopes=body.scopes,
        project_ids=body.project_ids,
        expires_in_days=body.expires_in_days,
    )
    await audit.record(
        ctx.session,
        ctx.actor,
        "api_key.created",
        org_id=org_id,
        subject_type="api_key",
        subject_id=created.key.id,
        metadata={"scopes": body.scopes, "name": body.name},
    )
    await ctx.session.refresh(created.key)
    return ApiKeyCreated(**_key_out(created.key).model_dump(), secret=created.secret)


@router.get("/orgs/{org_id}/api-keys", response_model=list[ApiKeyOut])
async def list_keys(org_id: uuid.UUID, ctx: CtxDep) -> list[ApiKeyOut]:
    require(ctx.principal, org_id, "admin")
    return [_key_out(k) for k in await service.list_api_keys(ctx.session, org_id)]


@router.delete("/orgs/{org_id}/api-keys/{key_id}", status_code=204)
async def revoke_key(org_id: uuid.UUID, key_id: uuid.UUID, ctx: CtxDep) -> Response:
    require(ctx.principal, org_id, "admin")
    if not await service.revoke_api_key(ctx.session, org_id, key_id):
        raise AppError("NOT_FOUND", "API key not found")
    await audit.record(
        ctx.session, ctx.actor, "api_key.revoked", org_id=org_id, subject_type="api_key", subject_id=key_id
    )
    return Response(status_code=204)
