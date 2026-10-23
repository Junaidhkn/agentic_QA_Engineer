from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from reliability.modules.artifacts import service as artifacts
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.runs import service
from reliability.modules.runs.models import RunMetricSummary
from reliability.modules.tenancy.policy import require
from reliability.platform import idempotency

router = APIRouter(tags=["runs"])


class EnvironmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["target_url", "sandbox_build"] = "target_url"
    target_id: uuid.UUID | None = None
    commit_sha: str | None = Field(default=None, pattern="^[0-9a-f]{40}$")


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["functional", "load", "probe"]
    suite_id: uuid.UUID | None = None
    load_profile_id: uuid.UUID | None = None
    environment: EnvironmentIn
    notes: str | None = Field(default=None, max_length=500)


class RunOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    status: str
    status_reason: str | None
    target_id: uuid.UUID | None
    suite_id: uuid.UUID | None
    load_profile_id: uuid.UUID | None
    environment_kind: str
    build_ref: dict[str, Any] | None
    requested_via: str
    notes: str | None
    profile: dict[str, Any] | None
    target: dict[str, Any] | None
    summary: dict[str, Any] | None
    generator_health: dict[str, Any] | None
    quota_reservation: dict[str, int]
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="stopped by user", max_length=200)


class MetricSummaryOut(BaseModel):
    scope: str
    time_window: str
    requests: int
    rps: float
    error_rate: float
    p50_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None


class ArtifactOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    size_bytes: int
    sensitivity: str
    expires_at: datetime


class VerdictOut(BaseModel):
    run_id: uuid.UUID
    status: str
    finished: bool
    passed: bool | None
    reason: str | None
    failed_thresholds: list[dict[str, Any]]


def out(r: service.Run) -> RunOut:
    cfg = r.config_snapshot or {}
    return RunOut(
        id=r.id,
        project_id=r.project_id,
        kind=r.kind,
        status=r.status,
        status_reason=r.status_reason,
        target_id=r.target_id,
        suite_id=r.suite_id,
        load_profile_id=r.load_profile_id,
        environment_kind=r.environment_kind,
        build_ref=r.build_ref,
        requested_via=r.requested_via,
        notes=r.notes,
        profile=cfg.get("profile"),
        target=cfg.get("target"),
        summary=r.summary,
        generator_health=r.generator_health,
        quota_reservation=r.quota_reservation or {},
        queued_at=r.queued_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
        created_at=r.created_at,
    )


@router.post("/projects/{project_id}/runs", status_code=202, response_model=RunOut)
async def create_run(
    project_id: uuid.UUID, body: RunCreate, ctx: CtxDep, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer", scope="runs:write")
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump(mode="json")
    if cached := await idempotency.lookup(ctx.session, project.org_id, key, payload):
        return cached.to_response()
    run = await service.create_run(
        ctx.session,
        ctx.principal,
        project,
        kind=body.kind,
        suite_id=body.suite_id,
        load_profile_id=body.load_profile_id,
        target_id=body.environment.target_id,
        environment_kind=body.environment.type,
        build_ref={"commit_sha": body.environment.commit_sha} if body.environment.commit_sha else None,
        notes=body.notes,
        idempotency_key=key,
    )
    result = out(run).model_dump(mode="json")
    await idempotency.store(ctx.session, project.org_id, key, payload, 202, result)
    return JSONResponse(result, status_code=202)


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
async def list_runs(
    project_id: uuid.UUID,
    ctx: CtxDep,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> list[RunOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return [
        out(r)
        for r in await service.list_for(
            ctx.session, project_id, kind=kind, status=status, limit=max(1, min(limit, 200)), before=before
        )
    ]


async def _load(ctx: Any, run_id: uuid.UUID, minimum: str = "viewer", scope: str = "runs:read") -> service.Run:
    run = await service.get(ctx.session, run_id)
    require(ctx.principal, run.org_id, minimum, scope=scope, project_id=run.project_id)
    return run


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, ctx: CtxDep) -> RunOut:
    return out(await _load(ctx, run_id))


@router.post("/runs/{run_id}:cancel", status_code=202, response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, ctx: CtxDep, body: CancelIn | None = None) -> RunOut:
    run = await _load(ctx, run_id, "engineer", "runs:write")
    return out(await service.cancel(ctx.session, ctx.actor, run, (body or CancelIn()).reason))


@router.get("/runs/{run_id}/verdict", response_model=VerdictOut)
async def verdict(run_id: uuid.UUID, ctx: CtxDep) -> VerdictOut:
    """CI-friendly verdict (FR-32): poll until ``finished`` and gate on ``passed``."""
    run = await _load(ctx, run_id)
    finished = run.status in ("passed", "failed", "invalid", "cancelled", "errored", "rejected")
    return VerdictOut(
        run_id=run.id,
        status=run.status,
        finished=finished,
        passed=(run.status == "passed") if finished else None,
        reason=run.status_reason,
        failed_thresholds=(run.summary or {}).get("failed_thresholds", []),
    )


@router.get("/runs/{run_id}/summaries", response_model=list[MetricSummaryOut])
async def summaries(run_id: uuid.UUID, ctx: CtxDep) -> list[MetricSummaryOut]:
    await _load(ctx, run_id)
    rows = (
        await ctx.session.execute(
            select(RunMetricSummary).where(RunMetricSummary.run_id == run_id).order_by(RunMetricSummary.scope)
        )
    ).scalars()
    return [
        MetricSummaryOut(
            scope=r.scope,
            time_window=r.time_window,
            requests=r.requests,
            rps=float(r.rps),
            error_rate=float(r.error_rate),
            p50_ms=_f(r.p50_ms),
            p90_ms=_f(r.p90_ms),
            p95_ms=_f(r.p95_ms),
            p99_ms=_f(r.p99_ms),
            max_ms=_f(r.max_ms),
        )
        for r in rows
    ]


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
async def run_artifacts(run_id: uuid.UUID, ctx: CtxDep) -> list[ArtifactOut]:
    await _load(ctx, run_id)
    return [
        ArtifactOut(
            id=a.id,
            kind=a.kind,
            name=a.storage_key.rsplit("/", 1)[1],
            size_bytes=a.size_bytes,
            sensitivity=a.sensitivity,
            expires_at=a.expires_at,
        )
        for a in await artifacts.for_run(ctx.session, run_id)
    ]


@router.get("/runs/{run_id}/events-log")
async def events_log(run_id: uuid.UUID, ctx: CtxDep, after: int = 0) -> list[dict[str, Any]]:
    await _load(ctx, run_id)
    return [
        {"seq": e.seq, "type": e.type, "payload": e.payload, "created_at": e.created_at.isoformat()}
        for e in await service.events_since(ctx.session, run_id, after)
    ]
