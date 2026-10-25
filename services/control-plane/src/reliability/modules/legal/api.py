from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from reliability.modules.identity.deps import CtxDep
from reliability.modules.legal import service
from reliability.modules.tenancy.policy import require

router = APIRouter(tags=["legal"])


class DocumentOut(BaseModel):
    document: str
    title: str
    version: str
    acceptable: bool
    required_for_runs: bool


class DocumentText(DocumentOut):
    markdown: str


class AcceptanceOut(BaseModel):
    document: str
    version: str
    accepted_at: datetime
    user_id: uuid.UUID


class LegalStatus(BaseModel):
    documents: list[DocumentOut]
    accepted: list[AcceptanceOut]
    missing_for_runs: list[str]


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: Literal["terms", "aup", "dpa"]
    version: str


def _doc(d: service.Document) -> DocumentOut:
    return DocumentOut(
        document=d.key, title=d.title, version=d.version, acceptable=d.acceptable, required_for_runs=d.required_for_runs
    )


@router.get("/legal/documents", response_model=list[DocumentOut])
async def documents() -> list[DocumentOut]:
    """Public: current versions of the Terms, AUP, DPA and sub-processor list."""
    return [_doc(d) for d in service.DOCUMENTS.values()]


@router.get("/legal/documents/{document}", response_model=DocumentText)
async def document_text(document: str) -> DocumentText:
    d = service.document(document)
    return DocumentText(**_doc(d).model_dump(), markdown=service.text_of(d.key))


async def _status(ctx: CtxDep, org_id: uuid.UUID) -> LegalStatus:
    rows = await service.accepted(ctx.session, org_id)
    return LegalStatus(
        documents=[_doc(d) for d in service.DOCUMENTS.values()],
        accepted=[
            AcceptanceOut(document=r.document, version=r.version, accepted_at=r.accepted_at, user_id=r.user_id)
            for r in rows
        ],
        missing_for_runs=await service.missing_for_runs(ctx.session, org_id),
    )


@router.get("/orgs/{org_id}/legal", response_model=LegalStatus)
async def legal_status(org_id: uuid.UUID, ctx: CtxDep) -> LegalStatus:
    require(ctx.principal, org_id, "viewer")
    return await _status(ctx, org_id)


@router.post("/orgs/{org_id}/legal-acceptances", response_model=LegalStatus)
async def accept(org_id: uuid.UUID, body: AcceptIn, request: Request, ctx: CtxDep) -> LegalStatus:
    """Owner/admin accepts a document version for the organization (recorded with IP and user agent)."""
    await service.accept(
        ctx.session,
        ctx.principal,
        org_id,
        body.document,
        body.version,
        request.client.host if request.client else None,
        request.headers.get("User-Agent"),
    )
    return await _status(ctx, org_id)
