"""Standalone sandbox builds and the preparation of ``sandbox_build`` runs (ADR-0011)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.environments import source as sources
from reliability.modules.environments.models import SandboxBuild
from reliability.modules.environments.plan import plan_for
from reliability.modules.identity.service import Principal
from reliability.modules.projects.models import Project
from reliability.modules.secrets import service as secrets
from reliability.platform import outbox
from reliability.platform.actor import Actor
from reliability.platform.crypto import seal_for_x25519
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.storage import get_store

BUILD_UPLOADS = ["log_bundle:sandbox.log", "telemetry_snapshot:sandbox.json"]
SANDBOX_SECRET_PREFIX = "SANDBOX_"
BUILD_LIMITS = {"cpu": 4, "mem_gb": 8, "pids": 4096}
BUILD_WALL_S = 1500


async def start_build(
    session: AsyncSession,
    principal: Principal,
    project: Project,
    *,
    commit_ref: str | None,
    archive_artifact_id: uuid.UUID | None,
) -> SandboxBuild:
    src = await sources.describe(session, project.id, commit_ref=commit_ref, archive_artifact_id=archive_artifact_id)
    build = SandboxBuild(
        id=uuid7(),
        org_id=project.org_id,
        project_id=project.id,
        status="queued",
        source=src,
        requested_by=principal.user_id,
    )
    session.add(build)
    await session.flush()
    await outbox.enqueue(
        session, "environments.plan_build", {"build_id": build.id, "org_id": project.org_id}, org_id=project.org_id
    )
    await audit.record(
        session,
        principal.actor,
        "sandbox_build.requested",
        org_id=project.org_id,
        subject_type="sandbox_build",
        subject_id=build.id,
        metadata={"source": src},
    )
    return build


async def get(session: AsyncSession, build_id: uuid.UUID) -> SandboxBuild:
    build = await session.get(SandboxBuild, build_id)
    if build is None:
        raise AppError("NOT_FOUND", "sandbox build not found")
    return build


async def list_for(session: AsyncSession, project_id: uuid.UUID, limit: int = 20) -> list[SandboxBuild]:
    rows = await session.execute(
        select(SandboxBuild)
        .where(SandboxBuild.project_id == project_id)
        .order_by(SandboxBuild.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def make_plan(
    session: AsyncSession, org_id: uuid.UUID, project_id: uuid.UUID, src: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """(commit sha, archive artifact id, plan) for a source."""
    names = {s.name for s in await secrets.list_names(session, project_id)}
    sha, art, snapshot = await sources.fetch(session, org_id, project_id, src)
    return sha, str(art.id), plan_for(snapshot, names).to_dict()


async def sandbox_lease_section(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    archive_artifact_id: str,
    plan: dict[str, Any],
    runner_id: str,
    runner_public_key: str,
    subject_id: uuid.UUID,
    ttl: int,
) -> dict[str, Any]:
    """The ``sandbox`` part of a lease: plan, presigned source URL and the app's own secrets
    (project secrets named ``SANDBOX_<NAME>``, delivered to the app as ``<NAME>``)."""
    art = await artifacts.get(session, uuid.UUID(archive_artifact_id))
    wanted = {f"{SANDBOX_SECRET_PREFIX}{n}" for n in plan.get("placeholders", [])}
    wanted |= {
        s.name for s in await secrets.list_names(session, project_id) if s.name.startswith(SANDBOX_SECRET_PREFIX)
    }
    values = (
        await secrets.resolve_for_run(session, Actor("runner", runner_id), org_id, project_id, wanted, subject_id)
        if wanted
        else {}
    )
    app_env = {k.removeprefix(SANDBOX_SECRET_PREFIX): v for k, v in values.items()}
    return {
        "plan": plan,
        "source_url": artifacts.download_url(art, ttl=ttl),
        "app_secrets_sealed": seal_for_x25519(runner_public_key, json.dumps(app_env).encode()),
    }


async def build_upload_urls(build: SandboxBuild, ttl: int) -> dict[str, str]:
    store = get_store()
    return {
        name: store.presign_put(
            artifacts.new_key(build.org_id, build.project_id, name.split(":", 1)[0], build.id, name.split(":", 1)[1]),
            ttl=ttl,
            content_type="text/plain" if name.endswith(".log") else "application/json",
        )
        for name in BUILD_UPLOADS
    }


async def register_build_uploads(session: AsyncSession, build: SandboxBuild, reported: list[dict[str, Any]]) -> None:
    store = get_store()
    for item in reported:
        name = item.get("name", "")
        if name not in BUILD_UPLOADS:
            continue
        kind, filename = name.split(":", 1)
        key = artifacts.new_key(build.org_id, build.project_id, kind, build.id, filename)
        if not store.exists(key):
            continue
        await artifacts.register(
            session,
            org_id=build.org_id,
            project_id=build.project_id,
            kind=kind,
            key=key,
            size=int(item.get("size", 0)),
            sha256=bytes.fromhex(item.get("sha256", "")) if item.get("sha256") else b"",
            content_type=item.get("content_type", "application/octet-stream"),
        )


def finish_build(build: SandboxBuild, body: dict[str, Any]) -> None:
    report = (body.get("result") or {}).get("sandbox") or {}
    ok = body.get("outcome") == "succeeded" and report.get("healthy") is True
    build.report = report
    build.status = "succeeded" if ok else ("cancelled" if body.get("outcome") == "cancelled" else "failed")
    build.error = None if ok else (report.get("error") or body.get("abort_reason") or "sandbox did not become healthy")
    build.finished_at = datetime.now(UTC)
