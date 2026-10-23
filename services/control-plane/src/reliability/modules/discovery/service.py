"""Discovery runs (FR-6…9), OpenAPI specs (FR-7) and source archives for repositories that
are not connected through GitHub (local development, evaluation harness)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.agents.discovery import openapi
from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.discovery.models import AgentThread, DiscoveryRun, OpenAPISpec
from reliability.modules.identity.service import Principal
from reliability.modules.integrations.github import service as github
from reliability.modules.projects.models import Project
from reliability.modules.workflows.models import Workflow
from reliability.platform import outbox
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

ACTIVE = ("queued", "running")
TERMINAL = ("awaiting_review", "completed", "failed", "cancelled", "budget_exhausted")
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
ARCHIVE_SHA = "0" * 40  # placeholder commit for uploaded archives (no git history)


async def register_openapi(
    session: AsyncSession, principal: Principal, project: Project, text: str, source: str, source_ref: str | None
) -> OpenAPISpec:
    try:
        ops = openapi.operations(openapi.load(text))
    except openapi.OpenAPIError as exc:
        raise AppError("VALIDATION_FAILED", f"invalid OpenAPI document: {exc}") from exc
    spec_id = uuid7()
    data = text.encode()
    art = await artifacts.put(
        session,
        org_id=project.org_id,
        project_id=project.id,
        kind="openapi_spec",
        owner_id=spec_id,
        name="openapi.txt",
        data=data,
        content_type="text/plain",
    )
    spec = OpenAPISpec(
        id=spec_id,
        org_id=project.org_id,
        project_id=project.id,
        source=source,
        source_ref=source_ref,
        artifact_key=art.storage_key,
        sha256=hashlib.sha256(data).digest(),
        operation_count=len(ops),
    )
    session.add(spec)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "openapi_spec.registered",
        org_id=project.org_id,
        subject_type="openapi_spec",
        subject_id=spec_id,
        metadata={"operations": len(ops)},
    )
    return spec


async def list_openapi(session: AsyncSession, project_id: uuid.UUID) -> list[OpenAPISpec]:
    rows = await session.execute(
        select(OpenAPISpec).where(OpenAPISpec.project_id == project_id).order_by(OpenAPISpec.created_at.desc())
    )
    return list(rows.scalars())


async def upload_archive(session: AsyncSession, principal: Principal, project: Project, data: bytes) -> str:
    """Store a .tar.gz/.zip of the source tree for discovery without GitHub; returns artifact id."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise AppError("VALIDATION_FAILED", "archive larger than 50 MB")
    if not (data[:2] == b"PK" or data[:2] == b"\x1f\x8b"):
        raise AppError("VALIDATION_FAILED", "archive must be .zip or .tar.gz")
    archive_id = uuid7()
    art = await artifacts.put(
        session,
        org_id=project.org_id,
        project_id=project.id,
        kind="code_snapshot",
        owner_id=archive_id,
        name="source.archive",
        data=data,
        content_type="application/octet-stream",
    )
    await audit.record(
        session,
        principal.actor,
        "source_archive.uploaded",
        org_id=project.org_id,
        subject_type="artifact",
        subject_id=art.id,
        metadata={"bytes": len(data)},
    )
    return str(art.id)


async def start(
    session: AsyncSession,
    principal: Principal,
    project: Project,
    *,
    commit_ref: str | None,
    openapi_spec_ids: list[uuid.UUID],
    archive_artifact_id: uuid.UUID | None,
) -> DiscoveryRun:
    active = (
        await session.execute(
            select(DiscoveryRun.id).where(DiscoveryRun.project_id == project.id, DiscoveryRun.status.in_(ACTIVE))
        )
    ).first()
    if active:
        raise AppError("CONFLICT", "a discovery is already running for this project", {"discovery_id": str(active[0])})
    options: dict[str, Any] = {"openapi_spec_ids": [str(s) for s in openapi_spec_ids]}
    if archive_artifact_id is not None:
        art = await artifacts.get(session, archive_artifact_id)
        if art.project_id != project.id or art.kind != "code_snapshot":
            raise AppError("NOT_FOUND", "source archive not found")
        options["archive_artifact_id"] = str(art.id)
    else:
        repo = await github.repository_for_project(session, project.id)
        if repo is None and not openapi_spec_ids:
            raise AppError("CONFLICT", "connect a repository, upload a source archive or register an OpenAPI spec")
        if repo is not None:
            options["repository_id"] = str(repo.id)
            options["commit_ref"] = commit_ref or repo.default_branch
    for spec_id in openapi_spec_ids:
        spec = await session.get(OpenAPISpec, spec_id)
        if spec is None or spec.project_id != project.id:
            raise AppError("NOT_FOUND", f"OpenAPI spec {spec_id} not found")
    run_id = uuid7()
    run = DiscoveryRun(
        id=run_id,
        org_id=project.org_id,
        project_id=project.id,
        commit_sha=ARCHIVE_SHA,
        options=options,
        status="queued",
        progress=[],
        agent_thread_id=f"discovery:{run_id}",
        requested_by=principal.user_id,
    )
    session.add(run)
    session.add(AgentThread(thread_id=run.agent_thread_id, org_id=project.org_id, graph="discovery", subject_id=run_id))
    await session.flush()
    await outbox.enqueue(
        session, "discovery.run", {"discovery_id": run_id, "org_id": project.org_id}, org_id=project.org_id
    )
    await audit.record(
        session,
        principal.actor,
        "discovery.started",
        org_id=project.org_id,
        subject_type="discovery_run",
        subject_id=run_id,
        metadata={"options": options},
    )
    return run


async def get(session: AsyncSession, discovery_id: uuid.UUID) -> DiscoveryRun:
    run = await session.get(DiscoveryRun, discovery_id)
    if run is None:
        raise AppError("NOT_FOUND", "discovery not found")
    return run


async def list_for(session: AsyncSession, project_id: uuid.UUID, limit: int = 20) -> list[DiscoveryRun]:
    rows = await session.execute(
        select(DiscoveryRun)
        .where(DiscoveryRun.project_id == project_id)
        .order_by(DiscoveryRun.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def cancel(session: AsyncSession, principal: Principal, run: DiscoveryRun) -> DiscoveryRun:
    if run.status not in ACTIVE:
        raise AppError("INVALID_STATE", f"discovery is {run.status}")
    run.status, run.finished_at = "cancelled", datetime.now(UTC)
    await audit.record(
        session,
        principal.actor,
        "discovery.cancelled",
        org_id=run.org_id,
        subject_type="discovery_run",
        subject_id=run.id,
    )
    return run


async def _passing(
    session: AsyncSession, project_id: uuid.UUID, suite_id: uuid.UUID, tested: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Tested workflows exercised by a completed functional run of this suite and without an
    open functional-failure finding."""
    from reliability.modules.findings.models import Finding
    from reliability.modules.runs.models import Run

    ran = await session.scalar(
        select(Run.id)
        .where(Run.suite_id == suite_id, Run.kind == "functional", Run.status.in_(("passed", "failed")))
        .limit(1)
    )
    if ran is None:
        return set()
    failing = set(
        (
            await session.execute(
                select(Finding.workflow_id).where(
                    Finding.project_id == project_id,
                    Finding.type == "functional_failure",
                    Finding.status.notin_(("resolved", "wont_fix", "duplicate")),
                )
            )
        ).scalars()
    )
    return tested - failing


async def coverage(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    """Discovered / approved / tested / passing counts by source and criticality (FR-9).
    Reported as counts — never as a percentage of "the application"."""
    from reliability.modules.testsuites import service as suites

    workflows = list((await session.execute(select(Workflow).where(Workflow.project_id == project_id))).scalars())
    suite = await suites.latest_ready(session, project_id)
    tested: set[uuid.UUID] = set()
    if suite is not None:
        tested = {
            c.workflow_id
            for c, _ in await suites.cases(session, suite.id)
            if c.validation_status in ("valid", "flaky", "pending")
        }
    passing = await _passing(session, project_id, suite.id, tested) if suite else set()
    out: dict[str, Any] = {
        "totals": {"discovered": 0, "approved": 0, "tested": 0, "passing": 0},
        "by_source": {},
        "by_criticality": {},
    }
    for w in workflows:
        if w.status in ("rejected", "retired"):
            continue
        flags = {
            "discovered": w.source != "manual",
            "approved": w.status == "approved",
            "tested": w.status == "approved" and w.id in tested,
            "passing": w.status == "approved" and w.id in passing,
        }
        for bucket in (
            out["totals"],
            out["by_source"].setdefault(w.source, {}),
            out["by_criticality"].setdefault(w.criticality, {}),
        ):
            for k, v in flags.items():
                bucket[k] = bucket.get(k, 0) + int(v)
    out["suite_version"] = suite.version if suite else None
    return out
