from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.approvals import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.tenancy.policy import default_org, require

router = APIRouter(tags=["approvals"])


class ApprovalOut(BaseModel):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    summary: dict[str, Any]
    requested_by: uuid.UUID | None
    decision: str | None
    decided_by: uuid.UUID | None
    reason: str | None
    expires_at: datetime
    created_at: datetime


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1000)


def _out(a: service.Approval) -> ApprovalOut:
    return ApprovalOut(
        id=a.id,
        subject_type=a.subject_type,
        subject_id=a.subject_id,
        summary=a.summary,
        requested_by=a.requested_by,
        decision=a.decision,
        decided_by=a.decided_by,
        reason=a.reason,
        expires_at=a.expires_at,
        created_at=a.created_at,
    )


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_pending(ctx: CtxDep, x_org_id: Annotated[str | None, Header()] = None) -> list[ApprovalOut]:
    org_id = default_org(ctx.principal, x_org_id)
    require(ctx.principal, org_id, "engineer")
    return [_out(a) for a in await service.pending(ctx.session, org_id)]


@router.post("/approvals/{approval_id}:decide", response_model=ApprovalOut)
async def decide(approval_id: uuid.UUID, body: DecisionIn, ctx: CtxDep) -> ApprovalOut:
    approval = await service.get(ctx.session, approval_id)
    require(ctx.principal, approval.org_id, "viewer")
    return _out(await service.decide(ctx.session, ctx.principal, approval, body.decision, body.reason))
