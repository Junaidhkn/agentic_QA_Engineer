"""Write-only project secrets (test credentials, API tokens for the app under test) — FR-11.

Envelope-encrypted with a per-secret data key; the encryption context binds each ciphertext
to its org, project and name, so a ciphertext copied elsewhere cannot be decrypted.
Plaintext is only produced for an execution lease (``resolve_for_run``) and never returned
by the API.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.secrets.models import ProjectSecret
from reliability.platform.actor import Actor
from reliability.platform.crypto import Sealed, seal, unseal
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_VALUE_BYTES = 8192


def _context(org_id: uuid.UUID, project_id: uuid.UUID, name: str) -> dict[str, str]:
    return {"org": str(org_id), "project": str(project_id), "name": name}


async def put(
    session: AsyncSession,
    actor: Actor,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    value: str,
    user_id: uuid.UUID,
) -> ProjectSecret:
    if not NAME_RE.match(name):
        raise AppError("VALIDATION_FAILED", "secret names are UPPER_SNAKE_CASE, 2-64 chars")
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise AppError("VALIDATION_FAILED", "secret value too large")
    sealed = seal(value, _context(org_id, project_id, name))
    existing = (
        await session.execute(
            select(ProjectSecret).where(ProjectSecret.project_id == project_id, ProjectSecret.name == name)
        )
    ).scalar_one_or_none()
    if existing:
        existing.ciphertext, existing.wrapped_dek = sealed.ciphertext, sealed.wrapped_dek
        existing.rotated_at = datetime.now(UTC)
        secret = existing
    else:
        secret = ProjectSecret(
            id=uuid7(),
            org_id=org_id,
            project_id=project_id,
            name=name,
            ciphertext=sealed.ciphertext,
            wrapped_dek=sealed.wrapped_dek,
            created_by=user_id,
        )
        session.add(secret)
    await session.flush()
    await audit.record(
        session,
        actor,
        "secret.written",
        org_id=org_id,
        subject_type="project_secret",
        subject_id=secret.id,
        metadata={"name": name, "rotated": existing is not None},
    )
    return secret


async def list_names(session: AsyncSession, project_id: uuid.UUID) -> list[ProjectSecret]:
    return list(
        (
            await session.execute(
                select(ProjectSecret).where(ProjectSecret.project_id == project_id).order_by(ProjectSecret.name)
            )
        ).scalars()
    )


async def remove(session: AsyncSession, actor: Actor, org_id: uuid.UUID, project_id: uuid.UUID, name: str) -> None:
    result = await session.execute(
        delete(ProjectSecret).where(ProjectSecret.project_id == project_id, ProjectSecret.name == name)
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        raise AppError("NOT_FOUND", "secret not found")
    await audit.record(
        session, actor, "secret.deleted", org_id=org_id, subject_type="project_secret", metadata={"name": name}
    )


async def resolve_for_run(
    session: AsyncSession,
    actor: Actor,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    names: set[str] | None = None,
    run_id: uuid.UUID | None = None,
) -> dict[str, str]:
    """Decrypt secrets for an execution lease. Audited; never call from request handlers."""
    out: dict[str, str] = {}
    for s in await list_names(session, project_id):
        if names is None or s.name in names:
            out[s.name] = unseal(Sealed(s.ciphertext, s.wrapped_dek), _context(org_id, project_id, s.name))
    if out:
        await audit.record(
            session,
            actor,
            "secret.resolved_for_run",
            org_id=org_id,
            subject_type="run",
            subject_id=run_id,
            metadata={"names": sorted(out)},
        )
    return out
