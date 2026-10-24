from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from reliability.modules.artifacts import service as artifacts
from reliability.modules.identity.deps import CtxDep, get_principal
from reliability.modules.identity.service import Principal
from reliability.modules.projects import service as projects
from reliability.modules.runs import service
from reliability.modules.runs.models import RunMetricSummary
from reliability.modules.tenancy.policy import require
from reliability.platform import idempotency
from reliability.platform.settings import get_settings

router = APIRouter(tags=["runs"])


class EnvironmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["target_url", "sandbox_build"] = "target_url"
    target_id: uuid.UUID | None = None
    commit_sha: str | None = Field(default=None, pattern="^[0-9a-f]{40}$")
    # sandbox_build: build from a branch/commit of the connected repository or an uploaded archive
    commit_ref: str | None = Field(default=None, max_length=200)
    archive_artifact_id: uuid.UUID | None = None


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


class VerdictFinding(BaseModel):
    id: uuid.UUID
    type: str
    severity: str
    title: str


class VerdictOut(BaseModel):
    """CI contract (FR-32, additive within /v1): gate on ``passed`` once ``finished``."""

    run_id: uuid.UUID
    status: str
    finished: bool
    passed: bool | None
    reason: str | None
    failed_thresholds: list[dict[str, Any]]
    summary: dict[str, Any] | None = None  # steady-state p95/p99, error rate, req/s
    new_findings: list[VerdictFinding] = []
    url: str | None = None


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
        build_ref={
            "commit_ref": body.environment.commit_sha or body.environment.commit_ref,
            "archive_artifact_id": str(body.environment.archive_artifact_id)
            if body.environment.archive_artifact_id
            else None,
        }
        if body.environment.type == "sandbox_build"
        else None,
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
    new = []
    if finished:
        from reliability.modules.findings.models import Finding

        new = [
            VerdictFinding(id=f.id, type=f.type, severity=f.severity, title=f.title)
            for f in (
                await ctx.session.execute(
                    select(Finding).where(Finding.first_run_id == run.id).order_by(Finding.severity)
                )
            ).scalars()
        ]
    web = get_settings().web_base_url.rstrip("/")
    return VerdictOut(
        run_id=run.id,
        status=run.status,
        finished=finished,
        passed=(run.status == "passed") if finished else None,
        reason=run.status_reason,
        failed_thresholds=(run.summary or {}).get("failed_thresholds", []),
        summary=(run.summary or {}).get("steady_state"),
        new_findings=new,
        url=f"{web}/projects/{run.project_id}/runs/{run.id}",
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


TERMINAL = {"passed", "failed", "invalid", "cancelled", "errored", "rejected"}


@router.get("/runs/{run_id}/metrics")
async def run_metrics(run_id: uuid.UUID, ctx: CtxDep, scope: str = "overall") -> dict[str, Any]:
    """Chart series from the run's time-series artifact (5 s buckets)."""
    import json as _json

    from reliability.modules.runs.timeseries import TimeSeries

    run = await _load(ctx, run_id)
    art = next((a for a in await artifacts.for_run(ctx.session, run.id) if a.kind == "k6_timeseries"), None)
    if art is None:
        return {"scope": scope, "scopes": [], "points": [], "steady_state": None}
    ts = TimeSeries(_json.loads(artifacts.read(art)))
    return {
        "scope": scope,
        "scopes": ["overall", *ts.endpoint_scopes()],
        "points": ts.series(scope),
        "steady_state": (run.summary or {}).get("steady_state"),
    }


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events (ADR-0021): replay from Postgres after ``Last-Event-ID``, then live via Redis."""
    import asyncio
    import json as _json

    from reliability.platform.db import tenant_tx
    from reliability.platform.redis_client import get_redis

    async with tenant_tx(user_id=principal.user_id, org_ids=principal.org_ids) as session:
        run = await service.get(session, run_id)
        require(principal, run.org_id, "viewer", scope="runs:read", project_id=run.project_id)
        org_ids = principal.org_ids

    def frame(seq: int, type_: str, payload: dict[str, Any]) -> str:
        return f"id: {seq}\nevent: {type_}\ndata: {_json.dumps(payload, default=str)}\n\n"

    async def generate() -> AsyncIterator[str]:
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(f"run:{run_id}")
        try:
            async with tenant_tx(org_ids=org_ids) as session:
                for e in await service.events_since(session, run_id, after):
                    yield frame(e.seq, e.type, e.payload)
                    after = e.seq
                status = (await service.get(session, run_id)).status
            if status in TERMINAL:
                yield frame(after, "end", {"status": status})
                return
            idle = 0.0
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    idle += 1.0
                    if idle >= 15:
                        idle = 0.0
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0)
                    continue
                data = _json.loads(msg["data"])
                if data["seq"] <= after:
                    continue
                after = data["seq"]
                yield frame(after, data["type"], data["payload"])
                if data["type"] == "status" and data["payload"].get("status") in TERMINAL:
                    yield frame(after, "end", {"status": data["payload"]["status"]})
                    return
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
