from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.artifacts import service as artifacts
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.reports import export, service
from reliability.modules.tenancy.policy import require
from reliability.platform import idempotency
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

router = APIRouter(tags=["reports"])
public_router = APIRouter(tags=["reports"])
SHARE_HEADERS = {
    # The shared page is static HTML: no scripts, no external loads, never framed or indexed.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "private, no-store",
}


@router.get("/projects/{project_id}/summary")
async def summary(project_id: uuid.UUID, ctx: CtxDep) -> dict[str, Any]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return await service.project_summary(ctx.session, project_id)


@router.get("/projects/{project_id}/trends")
async def trends(
    project_id: uuid.UUID, ctx: CtxDep, scope: str = "overall", metric: str = "p95_ms", limit: int = 30
) -> list[dict[str, Any]]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    try:
        return await service.trends(ctx.session, project_id, scope, metric, max(1, min(limit, 200)))
    except ValueError as exc:
        raise AppError("VALIDATION_FAILED", str(exc), {"metrics": list(service.TREND_METRICS)}) from exc


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    sections: list[str] | None = Field(default=None, max_length=len(export.SECTIONS))


class ShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_days: int = Field(default=7, ge=1, le=service.MAX_SHARE_DAYS)


class ReportOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    params: dict[str, Any]
    created_at: datetime
    html_url: str | None = None
    json_url: str | None = None
    shared: bool
    share_expires_at: datetime | None


class ShareOut(BaseModel):
    url: str
    expires_at: datetime


async def _out(ctx: Any, r: service.Report, urls: bool = False) -> ReportOut:
    html_url = json_url = None
    if urls and r.status == "ready" and r.html_artifact_id and r.json_artifact_id:
        html_url = artifacts.download_url(await artifacts.get(ctx.session, r.html_artifact_id))
        json_url = artifacts.download_url(await artifacts.get(ctx.session, r.json_artifact_id))
    return ReportOut(
        id=r.id,
        project_id=r.project_id,
        status=r.status,
        params=r.params,
        created_at=r.created_at,
        html_url=html_url,
        json_url=json_url,
        shared=r.share_token_hash is not None
        and bool(r.share_expires_at and r.share_expires_at > datetime.now(r.share_expires_at.tzinfo)),
        share_expires_at=r.share_expires_at,
    )


@router.post("/projects/{project_id}/reports", status_code=202, response_model=ReportOut)
async def create_report(
    project_id: uuid.UUID, body: ReportIn, ctx: CtxDep, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    project = await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    key = idempotency.require_key(idempotency_key)
    payload = {"project_id": str(project_id), "body": body.model_dump(mode="json", by_alias=True)}
    if cached := await idempotency.lookup(ctx.session, project.org_id, key, payload):
        return cached.to_response()
    await ratelimit.check(str(ctx.principal.user_id), "expensive")
    report = await service.create(
        ctx.session, ctx.principal, project.id, project.org_id, body.from_, body.to, body.sections
    )
    out = (await _out(ctx, report)).model_dump(mode="json")
    await idempotency.store(ctx.session, project.org_id, key, payload, 202, out)
    return JSONResponse(out, status_code=202)


@router.get("/projects/{project_id}/reports", response_model=list[ReportOut])
async def list_reports(project_id: uuid.UUID, ctx: CtxDep) -> list[ReportOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer")
    return [await _out(ctx, r) for r in await service.for_project(ctx.session, project_id)]


async def _report(ctx: Any, report_id: uuid.UUID, role: str = "viewer") -> service.Report:
    report = await service.get(ctx.session, report_id)
    require(ctx.principal, report.org_id, role, project_id=report.project_id)
    return report


@router.get("/reports/{report_id}", response_model=ReportOut)
async def get_report(report_id: uuid.UUID, ctx: CtxDep) -> ReportOut:
    return await _out(ctx, await _report(ctx, report_id), urls=True)


@router.post("/reports/{report_id}/share", response_model=ShareOut)
async def share_report(report_id: uuid.UUID, body: ShareIn, ctx: CtxDep) -> ShareOut:
    """Create or replace the share link (shown once). Anyone with the link can read the report."""
    report = await _report(ctx, report_id, "engineer")
    token, expires = await service.share(ctx.session, ctx.principal, report, body.expires_in_days)
    base = get_settings().api_base_url.rstrip("/")
    return ShareOut(url=f"{base}/shared/reports/{token}", expires_at=expires)


@router.delete("/reports/{report_id}/share", status_code=204)
async def revoke_share(report_id: uuid.UUID, ctx: CtxDep) -> Response:
    report = await _report(ctx, report_id, "engineer")
    await service.revoke_share(ctx.session, ctx.principal, report)
    return Response(status_code=204)


@public_router.get("/shared/reports/{token}", include_in_schema=False)
async def shared_report(token: str) -> Response:
    try:
        body = await service.shared_html(token)
    except AppError:
        return Response(
            "This link is invalid or has expired.", status_code=404, media_type="text/plain", headers=SHARE_HEADERS
        )
    return Response(body, media_type="text/html; charset=utf-8", headers=SHARE_HEADERS)
