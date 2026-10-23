from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.tenancy.policy import require
from reliability.modules.workflows import service
from reliability.modules.workflows.ir import validation_errors

router = APIRouter(tags=["workflows"])


class WorkflowOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    key: str
    title: str
    kind: str
    criticality: str
    source: str
    status: str
    discovery_confidence: Decimal | None
    destructive: bool
    spec: dict[str, Any]
    spec_version: int
    approved_at: datetime | None
    updated_at: datetime


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any]


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any] | None = None
    criticality: Literal["critical", "high", "normal"] | None = None
    status: Literal["approved", "rejected", "retired", "candidate"] | None = None


class BulkReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    reject: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class IrCheck(BaseModel):
    valid: bool
    errors: list[str]


def _out(w: service.Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=w.id,
        project_id=w.project_id,
        key=w.key,
        title=w.title,
        kind=w.kind,
        criticality=w.criticality,
        source=w.source,
        status=w.status,
        discovery_confidence=w.discovery_confidence,
        destructive=w.destructive,
        spec=w.spec,
        spec_version=w.spec_version,
        approved_at=w.approved_at,
        updated_at=w.updated_at,
    )


@router.get("/projects/{project_id}/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    project_id: uuid.UUID, ctx: CtxDep, status: str | None = None, kind: str | None = None, source: str | None = None
) -> list[WorkflowOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="projects:read")
    return [_out(w) for w in await service.list_for(ctx.session, project_id, status, kind, source)]


@router.post("/projects/{project_id}/workflows", status_code=201, response_model=WorkflowOut)
async def create_workflow(project_id: uuid.UUID, body: WorkflowCreate, ctx: CtxDep) -> WorkflowOut:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    return _out(await service.create_manual(ctx.session, ctx.principal, project, body.spec))


@router.post("/workflow-ir:validate", response_model=IrCheck)
async def check_ir(body: WorkflowCreate, ctx: CtxDep) -> IrCheck:
    errors = validation_errors(body.spec)
    return IrCheck(valid=not errors, errors=errors)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(workflow_id: uuid.UUID, ctx: CtxDep) -> WorkflowOut:
    wf = await service.get(ctx.session, workflow_id)
    require(ctx.principal, wf.org_id, "viewer", scope="projects:read", project_id=wf.project_id)
    return _out(wf)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
async def patch_workflow(workflow_id: uuid.UUID, body: WorkflowPatch, ctx: CtxDep) -> WorkflowOut:
    wf = await service.get(ctx.session, workflow_id)
    require(ctx.principal, wf.org_id, "engineer")
    return _out(
        await service.edit(
            ctx.session, ctx.principal, wf, spec=body.spec, criticality=body.criticality, status=body.status
        )
    )


@router.post("/projects/{project_id}/workflows:bulk-review")
async def bulk_review(project_id: uuid.UUID, body: BulkReview, ctx: CtxDep) -> dict[str, int]:
    await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    changed = await service.bulk_review(ctx.session, ctx.principal, project_id, body.approve, body.reject)
    return {"changed": changed}
