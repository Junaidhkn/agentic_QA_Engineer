"""Source code for a sandbox: an uploaded archive or a GitHub tarball at a commit, stored as a
short-lived ``code_snapshot`` artifact (24 h) that the runner downloads with a presigned URL."""

from __future__ import annotations

import io
import tarfile
import time
import uuid
import zipfile
from pathlib import PurePosixPath
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


def overlay_archive(data: bytes, files: dict[str, str | None]) -> bytes:
    """A .tar.gz of the source in ``data`` (tar or zip; a single top-level directory is stripped)
    with ``files`` applied: path -> new content, or None to delete. Used to build the base,
    test and patched variants of a repair without touching the repository."""
    members: dict[str, tuple[bytes, int]] = {}
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    members[info.filename] = (zf.read(info), 0o644)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for m in tf.getmembers():
                fh = tf.extractfile(m) if m.isfile() else None
                if fh is not None:
                    members[m.name] = (fh.read(), m.mode & 0o755 or 0o644)
    names = [n for n in members if not PurePosixPath(n).name.startswith("._")]
    tops = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
    strip = next(iter(tops)) + "/" if len(tops) == 1 and all("/" in n for n in names) else ""
    tree = {n.removeprefix(strip): members[n] for n in names}
    for path, content in files.items():
        if content is None:
            tree.pop(path, None)
        else:
            tree[path] = (content.encode(), tree.get(path, (b"", 0o644))[1])
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name in sorted(tree):
            body, mode = tree[name]
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(body), mode, int(time.time())
            tf.addfile(entry, io.BytesIO(body))
    return out.getvalue()
