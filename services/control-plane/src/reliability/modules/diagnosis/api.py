from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.diagnosis import service
from reliability.modules.diagnosis.models import Hypothesis
from reliability.modules.findings import service as findings
from reliability.modules.identity.deps import CtxDep
from reliability.modules.quotas import ratelimit
from reliability.modules.tenancy.policy import require
from reliability.platform import idempotency
from reliability.platform.errors import AppError

router = APIRouter(tags=["diagnosis"])


class CitationOut(BaseModel):
    id: uuid.UUID
    kind: str
    summary: str
    locator: dict[str, Any]
    relation: str
    claim: str
    check: str


class HypothesisOut(BaseModel):
    id: uuid.UUID
    rank: int
    category: str
    statement: str
    mechanism: str
    fix_direction: str | None
    code_locations: list[dict[str, Any]]
    confidence: Literal["high", "medium", "low"]
    confidence_factors: dict[str, Any]
    status: str
    user_verdict: str | None
    user_verdict_note: str | None
    evidence: list[CitationOut]


class DiagnosisOut(BaseModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    status: str
    hypotheses: list[HypothesisOut] | None = None
    next_steps: list[str]
    model_info: dict[str, Any] | None
    token_usage: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None
    disclaimer: str = service.DISCLAIMER


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["confirmed", "refuted", "unsure"]
    note: str | None = Field(default=None, max_length=1000)


def _out(d: service.Diagnosis, hyps: list[dict[str, Any]] | None = None) -> DiagnosisOut:
    return DiagnosisOut(
        id=d.id,
        finding_id=d.finding_id,
        status=d.status,
        hypotheses=None if hyps is None else [HypothesisOut.model_validate(h) for h in hyps],
        next_steps=list(d.next_steps or []),
        model_info=d.model_info,
        token_usage=d.token_usage,
        created_at=d.created_at,
        finished_at=d.finished_at,
    )


@router.post("/findings/{finding_id}/diagnoses", status_code=202, response_model=DiagnosisOut)
async def start_diagnosis(
    finding_id: uuid.UUID, ctx: CtxDep, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    finding = await findings.get(ctx.session, finding_id)
    require(ctx.principal, finding.org_id, "engineer", project_id=finding.project_id)
    key = idempotency.require_key(idempotency_key)
    payload = {"finding_id": str(finding_id)}
    if cached := await idempotency.lookup(ctx.session, finding.org_id, key, payload):
        return cached.to_response()
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    diag = await service.start(ctx.session, ctx.principal, finding)
    out = _out(diag).model_dump(mode="json")
    await idempotency.store(ctx.session, finding.org_id, key, payload, 202, out)
    return JSONResponse(out, status_code=202)


@router.get("/findings/{finding_id}/diagnoses", response_model=list[DiagnosisOut])
async def list_diagnoses(finding_id: uuid.UUID, ctx: CtxDep) -> list[DiagnosisOut]:
    finding = await findings.get(ctx.session, finding_id)
    require(ctx.principal, finding.org_id, "viewer", project_id=finding.project_id)
    return [_out(d) for d in await service.for_finding(ctx.session, finding_id)]


@router.get("/diagnoses/{diagnosis_id}", response_model=DiagnosisOut)
async def get_diagnosis(diagnosis_id: uuid.UUID, ctx: CtxDep) -> DiagnosisOut:
    diag = await service.get(ctx.session, diagnosis_id)
    finding = await findings.get(ctx.session, diag.finding_id)
    require(ctx.principal, diag.org_id, "viewer", project_id=finding.project_id)
    return _out(diag, await service.hypotheses(ctx.session, diagnosis_id))


@router.post("/hypotheses/{hypothesis_id}/feedback")
async def hypothesis_feedback(hypothesis_id: uuid.UUID, body: FeedbackIn, ctx: CtxDep) -> dict[str, Any]:
    h = await ctx.session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise AppError("NOT_FOUND", "hypothesis not found")
    require(ctx.principal, h.org_id, "engineer")
    h = await service.feedback(ctx.session, ctx.principal, hypothesis_id, body.verdict, body.note)
    return {"id": str(h.id), "user_verdict": h.user_verdict}


@router.get("/orgs/{org_id}/diagnosis-quality")
async def diagnosis_quality(org_id: uuid.UUID, ctx: CtxDep) -> dict[str, Any]:
    """Online precision from user feedback (evaluation §5)."""
    require(ctx.principal, org_id, "viewer")
    return await service.precision(ctx.session, org_id)
