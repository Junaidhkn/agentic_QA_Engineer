from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from reliability.modules.artifacts import service
from reliability.modules.identity.deps import CtxDep
from reliability.modules.tenancy.policy import require

router = APIRouter(tags=["artifacts"])


class DownloadUrl(BaseModel):
    url: str
    expires_in_s: int


@router.get("/artifacts/{artifact_id}/download", response_model=DownloadUrl)
async def download(artifact_id: uuid.UUID, ctx: CtxDep) -> DownloadUrl:
    """Short-lived presigned URL (JSON, so browser clients holding a bearer token can use it)."""
    art = await service.get(ctx.session, artifact_id)
    minimum = "engineer" if art.sensitivity != "normal" else "viewer"
    require(ctx.principal, art.org_id, minimum, scope="runs:read", project_id=art.project_id)
    return DownloadUrl(url=service.download_url(art, 300), expires_in_s=300)
