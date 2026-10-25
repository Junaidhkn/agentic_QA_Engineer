"""Artifact registry over object storage (ADR-0005). Keys are generated, never user-supplied."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts.models import Artifact

__all__ = ["Artifact"]
from reliability.modules.quotas.service import get_plan
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.storage import get_store, object_key

SOURCE_CODE_TTL = timedelta(hours=24)
DEFAULT_SENSITIVITY = {
    "playwright_trace": "may_contain_secrets",
    "har": "may_contain_secrets",
    "video": "may_contain_secrets",
    "screenshot": "may_contain_secrets",
    "log_bundle": "may_contain_secrets",
    "code_snapshot": "source_code",
    "patch": "source_code",
}
LONG_LIVED = {"test_bundle", "report_html", "report_json", "patch", "k6_summary", "openapi_spec"}


async def _expiry(session: AsyncSession, org_id: uuid.UUID, kind: str) -> datetime:
    now = datetime.now(UTC)
    if kind == "code_snapshot":
        return now + SOURCE_CODE_TTL
    if kind in LONG_LIVED:
        return now + timedelta(days=395)
    plan = await get_plan(session, org_id)
    return now + timedelta(days=plan.artifact_retention_days)


def new_key(org_id: uuid.UUID, project_id: uuid.UUID, kind: str, owner_id: uuid.UUID | str, name: str) -> str:
    return object_key(org_id, project_id, kind, owner_id, name)


async def put(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: str,
    owner_id: uuid.UUID | str,
    name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    run_id: uuid.UUID | None = None,
    repair_attempt_id: uuid.UUID | None = None,
) -> Artifact:
    key = new_key(org_id, project_id, kind, owner_id, name)
    get_store().put_bytes(key, data, content_type)
    return await register(
        session,
        org_id=org_id,
        project_id=project_id,
        kind=kind,
        key=key,
        size=len(data),
        sha256=hashlib.sha256(data).digest(),
        content_type=content_type,
        run_id=run_id,
        repair_attempt_id=repair_attempt_id,
    )


async def register(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: str,
    key: str,
    size: int,
    sha256: bytes,
    content_type: str = "application/octet-stream",
    run_id: uuid.UUID | None = None,
    repair_attempt_id: uuid.UUID | None = None,
) -> Artifact:
    if not key.startswith(f"org/{org_id}/project/{project_id}/"):
        raise AppError("FORBIDDEN", "artifact key outside the tenant prefix")
    art = Artifact(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        run_id=run_id,
        repair_attempt_id=repair_attempt_id,
        kind=kind,
        storage_key=key,
        size_bytes=size,
        sha256=sha256,
        content_type=content_type,
        sensitivity=DEFAULT_SENSITIVITY.get(kind, "normal"),
        expires_at=await _expiry(session, org_id, kind),
    )
    session.add(art)
    await session.flush()
    return art


async def replace(session: AsyncSession, art: Artifact, data: bytes, content_type: str) -> Artifact:
    """Overwrite an artifact's object in place (e.g. a test bundle recompiled after repair)."""
    get_store().put_bytes(art.storage_key, data, content_type)
    art.size_bytes, art.sha256, art.content_type = len(data), hashlib.sha256(data).digest(), content_type
    await session.flush()
    return art


async def get(session: AsyncSession, artifact_id: uuid.UUID) -> Artifact:
    art = await session.get(Artifact, artifact_id)
    if art is None or art.expires_at < datetime.now(UTC):
        raise AppError("NOT_FOUND", "artifact not found or expired")
    return art


def download_url(art: Artifact, ttl: int = 300) -> str:
    inline = art.kind in ("screenshot",)
    return get_store().presign_get(art.storage_key, ttl, filename=art.storage_key.rsplit("/", 1)[1], inline=inline)


def read(art: Artifact) -> bytes:
    return get_store().get_bytes(art.storage_key)


async def for_run(session: AsyncSession, run_id: uuid.UUID) -> list[Artifact]:
    return list(
        (
            await session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at))
        ).scalars()
    )
