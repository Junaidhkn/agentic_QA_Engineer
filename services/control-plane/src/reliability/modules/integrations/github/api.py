from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from reliability.modules.identity.deps import CtxDep
from reliability.modules.integrations.github import service
from reliability.modules.projects import service as projects
from reliability.modules.tenancy.policy import default_org, require
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

router = APIRouter(tags=["github"])


class InstallUrl(BaseModel):
    url: str


class RepoOut(BaseModel):
    installation_id: uuid.UUID
    github_repo_id: int
    full_name: str
    default_branch: str
    private: bool


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: uuid.UUID
    github_repo_id: int


class LinkedRepo(BaseModel):
    id: uuid.UUID
    full_name: str
    default_branch: str
    detected_stack: dict[str, object] | None


@router.post("/integrations/github/install-url", response_model=InstallUrl)
async def install_url(ctx: CtxDep, x_org_id: Annotated[str | None, Header()] = None) -> InstallUrl:
    org_id = default_org(ctx.principal, x_org_id)
    require(ctx.principal, org_id, "admin")
    return InstallUrl(url=service.install_url(ctx.principal, org_id))


@router.get("/integrations/github/callback", include_in_schema=False)
async def callback(
    installation_id: int, state: str, code: str | None = None, setup_action: str | None = None
) -> RedirectResponse:
    """Browser redirect from GitHub after installation (no bearer token; trust comes from ``state``)."""
    web = get_settings().web_base_url
    try:
        await service.complete_installation(installation_id, state, code)
    except AppError as exc:
        return RedirectResponse(f"{web}/settings/integrations?github=error&code={exc.code}", status_code=302)
    return RedirectResponse(f"{web}/settings/integrations?github=installed", status_code=302)


@router.get("/integrations/github/repositories", response_model=list[RepoOut])
async def repositories(ctx: CtxDep, x_org_id: Annotated[str | None, Header()] = None) -> list[RepoOut]:
    org_id = default_org(ctx.principal, x_org_id)
    require(ctx.principal, org_id, "admin")
    return [RepoOut(**r) for r in await service.list_repositories(ctx.session, org_id)]


@router.put("/projects/{project_id}/repository", response_model=LinkedRepo)
async def link(project_id: uuid.UUID, body: LinkIn, ctx: CtxDep) -> LinkedRepo:
    project = await projects.load(ctx.session, ctx.principal, project_id, "admin")
    repo = await service.link_repository(
        ctx.session,
        ctx.principal,
        org_id=project.org_id,
        project_id=project.id,
        installation_uuid=body.installation_id,
        github_repo_id=body.github_repo_id,
    )
    return LinkedRepo(
        id=repo.id, full_name=repo.full_name, default_branch=repo.default_branch, detected_stack=repo.detected_stack
    )


@router.get("/projects/{project_id}/repository", response_model=LinkedRepo | None)
async def get_link(project_id: uuid.UUID, ctx: CtxDep) -> LinkedRepo | None:
    await projects.load(ctx.session, ctx.principal, project_id, "viewer", scope="projects:read")
    repo = await service.repository_for_project(ctx.session, project_id)
    if repo is None:
        return None
    return LinkedRepo(
        id=repo.id, full_name=repo.full_name, default_branch=repo.default_branch, detected_stack=repo.detected_stack
    )


webhook_router = APIRouter(include_in_schema=False)


@webhook_router.post("/webhooks/github")
async def webhook(
    request: Request,
    x_github_event: Annotated[str, Header()],
    x_github_delivery: Annotated[str, Header()],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    body = await request.body()
    service.verify_signature(body, x_hub_signature_256)
    result = await service.handle_webhook(x_github_event, x_github_delivery, json.loads(body))
    return {"result": result}
