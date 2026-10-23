from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.artifacts import service as artifacts
from reliability.modules.artifacts.api import DownloadUrl
from reliability.modules.identity.deps import CtxDep
from reliability.modules.projects import service as projects
from reliability.modules.quotas import ratelimit
from reliability.modules.tenancy.policy import require
from reliability.modules.testsuites import service
from reliability.platform import idempotency

router = APIRouter(tags=["test-suites"])


class SuiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    # Dry-run each generated test (3×) against this verified target and repair failures (FR-10, FR-12).
    validate_target_id: uuid.UUID | None = None


class CaseOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_key: str
    type: str
    path: str
    validation_status: str
    validation_detail: dict[str, Any] | None


class SuiteOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: str
    commit_sha: str | None
    generator_info: dict[str, Any]
    created_at: datetime
    cases: list[CaseOut] | None = None


def _out(s: service.TestSuite, cases: list[CaseOut] | None = None) -> SuiteOut:
    return SuiteOut(
        id=s.id,
        project_id=s.project_id,
        version=s.version,
        status=s.status,
        commit_sha=s.commit_sha,
        generator_info=s.generator_info,
        created_at=s.created_at,
        cases=cases,
    )


async def _cases(ctx: Any, suite_id: uuid.UUID) -> list[CaseOut]:
    return [
        CaseOut(
            id=c.id,
            workflow_id=w.id,
            workflow_key=w.key,
            type=c.type,
            path=c.path,
            validation_status=c.validation_status,
            validation_detail=c.validation_detail,
        )
        for c, w in await service.cases(ctx.session, suite_id)
    ]


@router.post("/projects/{project_id}/test-suites", status_code=201, response_model=SuiteOut)
async def generate(
    project_id: uuid.UUID, body: SuiteCreate, ctx: CtxDep, idempotency_key: Annotated[str | None, Header()] = None
) -> Any:
    project = await projects.load(ctx.session, ctx.principal, project_id, "engineer")
    key = idempotency.require_key(idempotency_key)
    payload = body.model_dump(mode="json")
    if cached := await idempotency.lookup(ctx.session, project.org_id, key, payload):
        return cached.to_response()
    if body.validate_target_id:
        await ratelimit.check(str(ctx.principal.user_id), "expensive")
    suite = await service.generate(
        ctx.session, ctx.principal, project, body.workflow_ids, validate_target_id=body.validate_target_id
    )
    out = _out(suite, await _cases(ctx, suite.id))
    code = 202 if body.validate_target_id else 201
    await idempotency.store(ctx.session, project.org_id, key, payload, code, out.model_dump(mode="json"))
    return JSONResponse(out.model_dump(mode="json"), status_code=code)


@router.get("/projects/{project_id}/test-suites", response_model=list[SuiteOut])
async def list_suites(project_id: uuid.UUID, ctx: CtxDep) -> list[SuiteOut]:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="runs:read")
    return [_out(s) for s in await service.list_for(ctx.session, project_id)]


@router.get("/test-suites/{suite_id}", response_model=SuiteOut)
async def get_suite(suite_id: uuid.UUID, ctx: CtxDep) -> SuiteOut:
    suite = await service.get(ctx.session, suite_id)
    require(ctx.principal, suite.org_id, "viewer", scope="runs:read", project_id=suite.project_id)
    return _out(suite, await _cases(ctx, suite_id))


@router.get("/test-suites/{suite_id}/bundle", response_model=DownloadUrl)
async def bundle(suite_id: uuid.UUID, ctx: CtxDep, part: Literal["k6", "playwright"] = "k6") -> DownloadUrl:
    suite = await service.get(ctx.session, suite_id)
    require(ctx.principal, suite.org_id, "engineer")
    url = artifacts.download_url(await service.bundle_artifact(ctx.session, suite, part), 300)
    return DownloadUrl(url=url, expires_in_s=300)
