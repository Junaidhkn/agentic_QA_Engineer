from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.approvals import service as approvals
from reliability.modules.artifacts import service as artifacts
from reliability.modules.diagnosis.models import Diagnosis, Hypothesis
from reliability.modules.findings import service as findings
from reliability.modules.identity.deps import CtxDep
from reliability.modules.integrations.github import pr as github_pr
from reliability.modules.quotas import ratelimit
from reliability.modules.repairs import service
from reliability.modules.repairs.models import RepairAttempt
from reliability.modules.tenancy.policy import require
from reliability.modules.verification import service as verification
from reliability.platform import idempotency
from reliability.platform.errors import AppError

router = APIRouter(tags=["repairs"])


class ConstraintsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_files: int | None = Field(default=None, ge=1, le=10)
    max_changed_lines: int | None = Field(default=None, ge=1, le=300)
    allowed_paths: list[str] | None = Field(default=None, max_length=20)


class RepairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    constraints: ConstraintsIn | None = None


class RepairOut(BaseModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    hypothesis_id: uuid.UUID
    attempt_no: int
    status: str
    status_reason: str | None
    base_commit_sha: str
    constraints: dict[str, Any]
    diff_stats: dict[str, Any] | None
    regression_test_paths: list[str] | None
    created_at: datetime
    updated_at: datetime


class GateOut(BaseModel):
    gate: str
    status: str
    applicable: bool
    run_ids: list[str]
    result: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None


class ComparisonOut(BaseModel):
    scope: str
    metric: str
    baseline_value: float
    candidate_value: float
    delta_pct: float
    ci_low_pct: float
    ci_high_pct: float
    noise_floor_pct: float
    verdict: str
    method: str
    baseline_run_ids: list[str]
    candidate_run_ids: list[str]


class VerificationOut(BaseModel):
    id: uuid.UUID
    status: str
    summary: dict[str, Any] | None
    gates: list[GateOut]
    comparisons: list[ComparisonOut]
    created_at: datetime
    finished_at: datetime | None


class ApprovalRef(BaseModel):
    id: uuid.UUID
    decision: str | None
    reason: str | None
    expires_at: datetime


class PullRequestOut(BaseModel):
    state: str
    url: str | None
    number: int | None
    branch: str
    error: str | None


class RepairDetail(RepairOut):
    agent_summary: str | None
    agent_summary_label: str = "AI-generated explanation"
    diff: str | None
    verification: VerificationOut | None
    approval: ApprovalRef | None
    pull_request: PullRequestOut | None
    token_usage: dict[str, Any] | None


def _out(a: RepairAttempt) -> RepairOut:
    return RepairOut(
        id=a.id,
        finding_id=a.finding_id,
        hypothesis_id=a.hypothesis_id,
        attempt_no=a.attempt_no,
        status=a.status,
        status_reason=a.status_reason,
        base_commit_sha=a.base_commit_sha,
        constraints=a.constraints,
        diff_stats=a.diff_stats,
        regression_test_paths=a.regression_test_paths,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


async def _attempt(ctx: Any, attempt_id: uuid.UUID, role: str = "viewer") -> RepairAttempt:
    attempt = await service.get(ctx.session, attempt_id)
    require(ctx.principal, attempt.org_id, role, project_id=attempt.project_id)
    return attempt


@router.post("/hypotheses/{hypothesis_id}/repairs", status_code=202, response_model=RepairOut)
async def start_repair(
    hypothesis_id: uuid.UUID,
    body: RepairIn,
    ctx: CtxDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Any:
    h = await ctx.session.get(Hypothesis, hypothesis_id)
    diag = await ctx.session.get(Diagnosis, h.diagnosis_id) if h else None
    if h is None or diag is None:
        raise AppError("NOT_FOUND", "hypothesis not found")
    finding = await findings.get(ctx.session, diag.finding_id)
    require(ctx.principal, finding.org_id, "engineer", project_id=finding.project_id)
    key = idempotency.require_key(idempotency_key)
    payload = {"hypothesis_id": str(hypothesis_id), "body": body.model_dump(mode="json")}
    if cached := await idempotency.lookup(ctx.session, finding.org_id, key, payload):
        return cached.to_response()
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    constraints = body.constraints.model_dump(exclude_none=True) if body.constraints else None
    attempt = await service.start(ctx.session, ctx.principal, hypothesis_id, constraints)
    out = _out(attempt).model_dump(mode="json")
    await idempotency.store(ctx.session, finding.org_id, key, payload, 202, out)
    return JSONResponse(out, status_code=202)


@router.get("/findings/{finding_id}/repairs", response_model=list[RepairOut])
async def list_repairs(finding_id: uuid.UUID, ctx: CtxDep) -> list[RepairOut]:
    finding = await findings.get(ctx.session, finding_id)
    require(ctx.principal, finding.org_id, "viewer", project_id=finding.project_id)
    return [_out(a) for a in await service.for_finding(ctx.session, finding_id)]


@router.get("/repairs/{attempt_id}", response_model=RepairDetail)
async def get_repair(attempt_id: uuid.UUID, ctx: CtxDep) -> RepairDetail:
    a = await _attempt(ctx, attempt_id)
    diff = None
    if a.patch_artifact_id:
        diff = artifacts.read(await artifacts.get(ctx.session, a.patch_artifact_id)).decode(errors="replace")[:400_000]
    ver = await verification.for_attempt(ctx.session, a.id)
    appr = await approvals.for_subject(ctx.session, "repair_pr", a.id)
    pr = await github_pr.existing(ctx.session, a.id)
    return RepairDetail(
        **_out(a).model_dump(),
        agent_summary=a.agent_summary,
        diff=diff,
        verification=VerificationOut.model_validate(ver) if ver else None,
        approval=ApprovalRef(id=appr.id, decision=appr.decision, reason=appr.reason, expires_at=appr.expires_at)
        if appr
        else None,
        pull_request=PullRequestOut(state=pr.state, url=pr.url, number=pr.number, branch=pr.branch, error=pr.error)
        if pr
        else None,
        token_usage=a.token_usage,
    )


@router.get("/repairs/{attempt_id}/patch", response_class=Response)
async def download_patch(attempt_id: uuid.UUID, ctx: CtxDep) -> Response:
    a = await _attempt(ctx, attempt_id)
    if not a.patch_artifact_id:
        raise AppError("NOT_FOUND", "this repair has no patch yet")
    data = artifacts.read(await artifacts.get(ctx.session, a.patch_artifact_id))
    return Response(
        data,
        media_type="text/x-diff",
        headers={"Content-Disposition": f'attachment; filename="repair-{str(a.id)[:8]}.diff"'},
    )


@router.post("/repairs/{attempt_id}:cancel", response_model=RepairOut)
async def cancel_repair(attempt_id: uuid.UUID, ctx: CtxDep) -> RepairOut:
    a = await _attempt(ctx, attempt_id, "engineer")
    return _out(await service.cancel(ctx.session, ctx.principal, a))
