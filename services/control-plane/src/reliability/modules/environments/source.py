"""Source code for a sandbox: an uploaded archive or a GitHub tarball at a commit, stored as a
short-lived ``code_snapshot`` artifact (24 h) that the runner downloads with a presigned URL."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reliability.agents.discovery.snapshot import CodeSnapshot, from_archive
from reliability.modules.artifacts import service as artifacts
from reliability.modules.artifacts.models import Artifact
from reliability.modules.integrations.github import service as github
from reliability.modules.integrations.github.models import Repository
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

ARCHIVE_SHA = "0" * 40


async def describe(
    session: AsyncSession, project_id: uuid.UUID, *, commit_ref: str | None, archive_artifact_id: uuid.UUID | None
) -> dict[str, Any]:
    """Validate the requested source when a run/build is created (no download yet)."""
    if archive_artifact_id is not None:
        art = await artifacts.get(session, archive_artifact_id)
        if art.project_id != project_id or art.kind != "code_snapshot":
            raise AppError("NOT_FOUND", "source archive not found")
        return {"type": "archive", "archive_artifact_id": str(art.id)}
    repo = await github.repository_for_project(session, project_id)
    if repo is None:
        raise AppError("CONFLICT", "connect a repository or upload a source archive to build a sandbox")
    return {"type": "repository", "repository_id": str(repo.id), "ref": commit_ref or repo.default_branch}


async def fetch(
    session: AsyncSession, org_id: uuid.UUID, project_id: uuid.UUID, source: dict[str, Any]
) -> tuple[str, Artifact, CodeSnapshot]:
    """Resolve the source to (commit sha, archive artifact, parsed snapshot)."""
    if source["type"] == "archive":
        art = await session.get(Artifact, uuid.UUID(source["archive_artifact_id"]))
        if art is None:
            raise AppError("NOT_FOUND", "source archive expired; upload it again")
        return ARCHIVE_SHA, art, from_archive(artifacts.read(art))
    repo = await session.get(Repository, uuid.UUID(source["repository_id"]))
    if repo is None:
        raise AppError("CONFLICT", "repository is no longer connected")
    sha, data = await github.snapshot(session, repo, source["ref"])
    art = await artifacts.put(
        session,
        org_id=org_id,
        project_id=project_id,
        kind="code_snapshot",
        owner_id=uuid7(),
        name=f"{sha}.tar.gz",
        data=data,
        content_type="application/gzip",
    )
    return sha, art, from_archive(data)
