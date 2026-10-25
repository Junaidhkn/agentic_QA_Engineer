from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from reliability.modules.audit import service as audit
from reliability.modules.findings import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.tenancy.policy import require

router = APIRouter(tags=["findings"])


class EvidenceOut(BaseModel):
    id: uuid.UUID
    kind: str
    locator: dict[str, Any]
    summary: str
    snapshot: dict[str, Any] | None
    collected_by: str
    run_id: uuid.UUID | None
    created_at: datetime


class FindingOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    type: str
    severity: str
    title: str
    status: str
    fingerprint: str
    workflow_id: uuid.UUID | None
    first_run_id: uuid.UUID
    last_run_id: uuid.UUID
    resolved_by_run_id: uuid.UUID | None
    occurrences: int
    headline: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    evidence: list[EvidenceOut] | None = None


class FindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "wont_fix", "duplicate", "resolved"]


def ev_out(e: service.Evidence) -> EvidenceOut:
    return EvidenceOut(
        id=e.id,
        kind=e.kind,
        locator=e.locator,
        summary=e.summary,
        snapshot=e.snapshot,
        collected_by=e.collected_by,
        run_id=e.run_id,
        created_at=e.created_at,
    )


def out(f: service.Finding, evidence: list[EvidenceOut] | None = None) -> FindingOut:
    return FindingOut(
        id=f.id,
        project_id=f.project_id,
        type=f.type,
        severity=f.severity,
        title=f.title,
        status=f.status,
        fingerprint=f.fingerprint,
        workflow_id=f.workflow_id,
        first_run_id=f.first_run_id,
        last_run_id=f.last_run_id,
        resolved_by_run_id=f.resolved_by_run_id,
        occurrences=f.occurrences,
        headline=f.headline,
        created_at=f.created_at,
        updated_at=f.updated_at,
        evidence=evidence,
    )


@router.get("/projects/{project_id}/findings", response_model=list[FindingOut])
async def list_findings(
    project_id: uuid.UUID, ctx: CtxDep, status: str | None = None, type: str | None = None
) -> list[FindingOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return [out(f) for f in await service.list_for(ctx.session, project_id, status, type)]


@router.get("/findings/{finding_id}", response_model=FindingOut)
async def get_finding(finding_id: uuid.UUID, ctx: CtxDep) -> FindingOut:
    f = await service.get(ctx.session, finding_id)
    require(ctx.principal, f.org_id, "viewer", scope="runs:read", project_id=f.project_id)
    return out(f, [ev_out(e) for e in await service.evidence_for(ctx.session, finding_id)])


@router.patch("/findings/{finding_id}", response_model=FindingOut)
async def triage(finding_id: uuid.UUID, body: FindingPatch, ctx: CtxDep) -> FindingOut:
    f = await service.get(ctx.session, finding_id)
    require(ctx.principal, f.org_id, "engineer")
    old, f.status = f.status, body.status
    await audit.record(
        ctx.session,
        ctx.actor,
        "finding.triaged",
        org_id=f.org_id,
        subject_type="finding",
        subject_id=f.id,
        metadata={"from": old, "to": body.status},
    )
    await ctx.session.flush()
    return out(f)
