"""Legal documents and their acceptance (NFR-14, threat model A1).

An owner or admin accepts the current Terms and Acceptable Use Policy for the organization
before it can run tests — the AUP is the explicit load-testing consent. The DPA can be
accepted on request (it is part of the Terms either way). A new document version requires a
new acceptance; old rows stay as the record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.legal.models import LegalAcceptance
from reliability.modules.tenancy.policy import require
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

DOCS = Path(__file__).parent / "documents"


@dataclass(frozen=True)
class Document:
    key: str
    title: str
    version: str
    acceptable: bool  # recorded per organization
    required_for_runs: bool


DOCUMENTS = {
    d.key: d
    for d in (
        Document("terms", "Terms of Service", "2026-10-25", True, True),
        Document("aup", "Acceptable Use Policy", "2026-10-25", True, True),
        Document("dpa", "Data Processing Addendum", "2026-10-25", True, False),
        Document("subprocessors", "Sub-processors", "2026-10-25", False, False),
    )
}


def document(key: str) -> Document:
    if key not in DOCUMENTS:
        raise AppError("NOT_FOUND", "document not found")
    return DOCUMENTS[key]


def text_of(key: str) -> str:
    return (DOCS / f"{document(key).key}.md").read_text()


async def accepted(session: AsyncSession, org_id: uuid.UUID) -> list[LegalAcceptance]:
    rows = await session.execute(
        select(LegalAcceptance).where(LegalAcceptance.org_id == org_id).order_by(LegalAcceptance.accepted_at)
    )
    return list(rows.scalars())


async def missing_for_runs(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Current required documents this organization has not accepted."""
    have = {(a.document, a.version) for a in await accepted(session, org_id)}
    return [d.key for d in DOCUMENTS.values() if d.required_for_runs and (d.key, d.version) not in have]


async def require_for_runs(session: AsyncSession, org_id: uuid.UUID) -> None:
    missing = await missing_for_runs(session, org_id)
    if missing:
        titles = ", ".join(DOCUMENTS[m].title for m in missing)
        raise AppError(
            "TERMS_NOT_ACCEPTED",
            f"an owner or admin must accept the current {titles} before tests can run",
            {"missing": missing, "versions": {m: DOCUMENTS[m].version for m in missing}},
        )


async def accept(
    session: AsyncSession,
    principal: Principal,
    org_id: uuid.UUID,
    key: str,
    version: str,
    ip: str | None,
    user_agent: str | None,
) -> LegalAcceptance:
    doc = document(key)
    require(principal, org_id, "admin")
    if principal.user_id is None:
        raise AppError("FORBIDDEN", "documents are accepted by a person, not an API key")
    if not doc.acceptable:
        raise AppError("VALIDATION_FAILED", f"{doc.title} is informational and is not accepted")
    if version != doc.version:
        raise AppError("CONFLICT", f"the current version of the {doc.title} is {doc.version}")
    existing = (
        await session.execute(
            select(LegalAcceptance).where(
                LegalAcceptance.org_id == org_id, LegalAcceptance.document == key, LegalAcceptance.version == version
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = LegalAcceptance(
        id=uuid7(),
        org_id=org_id,
        user_id=principal.user_id,
        document=key,
        version=version,
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "legal.accepted",
        org_id=org_id,
        subject_type="legal_document",
        subject_id=key,
        metadata={"version": version},
    )
    return row
