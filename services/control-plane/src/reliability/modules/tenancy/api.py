from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from reliability.modules.audit.models import AuditEvent
from reliability.modules.identity.deps import CtxDep
from reliability.modules.quotas import service as quotas
from reliability.modules.tenancy import service
from reliability.modules.tenancy.policy import require
from reliability.platform.db import set_tenant

router = APIRouter(tags=["organizations"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrgCreate(Strict):
    name: str = Field(min_length=2, max_length=80)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_id: str
    role: str
    suspended: bool
    llm_code_context_enabled: bool


class OrgPatch(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    llm_code_context_enabled: bool | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    joined_at: datetime


class InviteIn(Strict):
    email: EmailStr
    role: Literal["admin", "engineer", "viewer"]


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime


class RoleIn(Strict):
    role: Literal["owner", "admin", "engineer", "viewer"]


class UsageOut(BaseModel):
    plan_id: str
    usage: dict[str, dict[str, int]]
    limits: dict[str, int | list[str]]


class AuditEventOut(BaseModel):
    seq: int
    created_at: datetime
    actor_type: str
    actor_id: str
    action: str
    subject_type: str | None
    subject_id: str | None
    ip: str | None
    request_id: str | None
    metadata: dict[str, object]


class AuditPage(BaseModel):
    data: list[AuditEventOut]
    next_cursor: str | None


def _org_out(org: service.Organization, role: str) -> OrgOut:
    return OrgOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        plan_id=org.plan_id,
        role=role,
        suspended=org.suspended_at is not None,
        llm_code_context_enabled=org.llm_code_context_enabled,
    )


@router.post("/orgs", status_code=201, response_model=OrgOut)
async def create_org(body: OrgCreate, ctx: CtxDep) -> OrgOut:
    org = await service.create_org(ctx.session, ctx.principal, body.name)
    return _org_out(org, "owner")


@router.get("/orgs", response_model=list[OrgOut])
async def list_orgs(ctx: CtxDep) -> list[OrgOut]:
    return [_org_out(o, role) for o, role in await service.list_orgs(ctx.session, ctx.principal)]


@router.get("/orgs/{org_id}", response_model=OrgOut)
async def get_org(org_id: uuid.UUID, ctx: CtxDep) -> OrgOut:
    require(ctx.principal, org_id, "viewer", scope="projects:read")
    return _org_out(await service.get_org(ctx.session, org_id), ctx.principal.org_roles[org_id])


@router.patch("/orgs/{org_id}", response_model=OrgOut)
async def patch_org(org_id: uuid.UUID, body: OrgPatch, ctx: CtxDep) -> OrgOut:
    require(ctx.principal, org_id, "owner")
    org = await service.get_org(ctx.session, org_id)
    if body.name is not None:
        org.name = body.name
    if body.llm_code_context_enabled is not None:
        org.llm_code_context_enabled = body.llm_code_context_enabled
    await service.audit.record(
        ctx.session,
        ctx.actor,
        "org.updated",
        org_id=org_id,
        subject_type="organization",
        subject_id=org_id,
        metadata=body.model_dump(exclude_none=True),
    )
    return _org_out(org, ctx.principal.org_roles[org_id])


@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(org_id: uuid.UUID, ctx: CtxDep) -> list[MemberOut]:
    require(ctx.principal, org_id, "admin")
    return [
        MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role, joined_at=m.created_at)
        for m, u in await service.list_members(ctx.session, org_id)
    ]


@router.post("/orgs/{org_id}/invitations", status_code=201, response_model=InviteOut)
async def invite(org_id: uuid.UUID, body: InviteIn, ctx: CtxDep) -> InviteOut:
    require(ctx.principal, org_id, "admin")
    inv = await service.invite(ctx.session, ctx.principal, org_id, body.email, body.role)
    return InviteOut(id=inv.id, email=inv.email, role=inv.role, expires_at=inv.expires_at)


@router.post("/invitations/{token}:accept", response_model=OrgOut)
async def accept(token: str, ctx: CtxDep) -> OrgOut:
    org_id, role = await service.accept_invitation(ctx.principal, token)
    ctx.principal.org_roles[org_id] = role
    await set_tenant(ctx.session, ctx.principal.user_id, ctx.principal.org_ids)
    org = await service.get_org(ctx.session, org_id)
    return _org_out(org, ctx.principal.org_roles[org_id])


@router.patch("/orgs/{org_id}/members/{user_id}", response_model=MemberOut)
async def change_role(org_id: uuid.UUID, user_id: uuid.UUID, body: RoleIn, ctx: CtxDep) -> MemberOut:
    require(ctx.principal, org_id, "owner")
    m = await service.change_role(ctx.session, ctx.principal, org_id, user_id, body.role)
    members = {u.id: u for _, u in await service.list_members(ctx.session, org_id)}
    u = members[user_id]
    return MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role, joined_at=m.created_at)


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204)
async def remove_member(org_id: uuid.UUID, user_id: uuid.UUID, ctx: CtxDep) -> Response:
    require(ctx.principal, org_id, "admin")
    await service.remove_member(ctx.session, ctx.principal, org_id, user_id)
    return Response(status_code=204)


@router.get("/orgs/{org_id}/usage", response_model=UsageOut)
async def usage(org_id: uuid.UUID, ctx: CtxDep) -> UsageOut:
    require(ctx.principal, org_id, "admin")
    plan = await quotas.get_plan(ctx.session, org_id)
    return UsageOut(
        plan_id=plan.id,
        usage=await quotas.usage_summary(ctx.session, org_id),
        limits={
            "max_vus": plan.max_vus,
            "max_duration_s": plan.max_duration_s,
            "max_concurrent_runs": plan.max_concurrent_runs,
            "max_projects": plan.max_projects,
            "allowed_profile_types": plan.allowed_profile_types,
        },
    )


@router.get("/orgs/{org_id}/audit-events", response_model=AuditPage)
async def audit_events(
    org_id: uuid.UUID, ctx: CtxDep, limit: int = 50, cursor: str | None = None, action: str | None = None
) -> AuditPage:
    from sqlalchemy import select

    require(ctx.principal, org_id, "admin")
    limit = max(1, min(limit, 200))
    stmt = select(AuditEvent).where(AuditEvent.org_id == org_id).order_by(AuditEvent.seq.desc()).limit(limit + 1)
    if cursor:
        stmt = stmt.where(AuditEvent.seq < int(cursor))
    if action:
        stmt = stmt.where(AuditEvent.action.like(action.replace("*", "%")))
    rows = list((await ctx.session.execute(stmt)).scalars())
    page = rows[:limit]
    return AuditPage(
        data=[
            AuditEventOut(
                seq=r.seq,
                created_at=r.created_at,
                actor_type=r.actor_type,
                actor_id=r.actor_id,
                action=r.action,
                subject_type=r.subject_type,
                subject_id=r.subject_id,
                ip=str(r.ip) if r.ip else None,
                request_id=r.request_id,
                metadata=r.metadata_,
            )
            for r in page
        ],
        next_cursor=str(page[-1].seq) if len(rows) > limit else None,
    )
