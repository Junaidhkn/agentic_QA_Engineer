"""Background jobs for sandboxes: plan standalone builds and prepare ``sandbox_build`` runs;
the ``sandbox_build`` runner job type (build → start → migrate/seed → health → teardown)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.environments import service
from reliability.modules.environments.models import SandboxBuild
from reliability.modules.runners import service as runners
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import ExecutionJob, Run
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.jobs import job

log = logging.getLogger(__name__)


@job("environments.plan_build")
async def plan_build(payload: dict[str, Any]) -> None:
    org_id, build_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["build_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        build = await s.get(SandboxBuild, build_id, with_for_update=True)
        if build is None or build.status != "queued":
            return
        build.status = "planning"
    plan: dict[str, Any] | None = None
    sha = archive_id = ""
    try:
        async with tenant_tx(org_ids=[org_id]) as s:
            sha, archive_id, plan = await service.make_plan(s, org_id, build.project_id, build.source)
        error = plan["unsupported"]
    except AppError as exc:
        error = exc.message
    async with tenant_tx(org_ids=[org_id]) as s:
        build = await s.get(SandboxBuild, build_id, with_for_update=True)
        assert build is not None
        if build.status == "cancelled":
            return
        build.plan = plan
        if error:
            build.status, build.error, build.finished_at = "failed", error, datetime.now(UTC)
            return
        build.commit_sha, build.status = sha, "building"
        build.source = {**build.source, "archive_artifact_id": archive_id}
        await runners.enqueue_job(
            s, org_id=org_id, job_type="sandbox_build", subject_id=build.id, spec={"build_id": str(build.id)}
        )


@job("environments.prepare_run")
async def prepare_run(payload: dict[str, Any]) -> None:
    """Fetch the source and plan the sandbox; the run becomes leasable once ``build_ref.status``
    is ``ready`` (the runner then builds and starts it before testing)."""
    org_id, run_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["run_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(Run, run_id)
        if run is None or run.environment_kind != "sandbox_build" or (run.build_ref or {}).get("status") != "pending":
            return
        src, project_id = dict((run.build_ref or {})["source"]), run.project_id
    plan: dict[str, Any] | None = None
    sha = archive_id = ""
    try:
        async with tenant_tx(org_ids=[org_id]) as s:
            sha, archive_id, plan = await service.make_plan(s, org_id, project_id, src)
        error = plan["unsupported"]
    except AppError as exc:
        error = exc.message
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await runs.lock_for_update(s, run_id)
        ref = dict(run.build_ref or {})
        if ref.get("status") != "pending":
            return
        if error or plan is None:
            run.build_ref = {**ref, "status": "failed", "plan": plan, "error": error}
            await runs.reject_environment(s, run, f"sandbox cannot be built: {error}")
            return
        run.build_ref = {
            **ref,
            "status": "ready",
            "commit_sha": sha,
            "archive_artifact_id": archive_id,
            "plan": plan,
        }
        await runs.add_event(
            s, run, "sandbox", {"phase": "planned", "strategy": plan["strategy"], "services": sorted(plan["services"])}
        )


async def _build_spec(
    session: AsyncSession, jb: ExecutionJob, lease_id: str, runner_id: str, public_key: str
) -> dict[str, Any]:
    build = await service.get(session, uuid.UUID(jb.spec["build_id"]))
    if build.plan is None or "archive_artifact_id" not in build.source:
        raise AppError("CONFLICT", "build has no plan")
    sandbox = await service.sandbox_lease_section(
        session,
        org_id=build.org_id,
        project_id=build.project_id,
        archive_artifact_id=build.source["archive_artifact_id"],
        plan=build.plan,
        runner_id=runner_id,
        runner_public_key=public_key,
        subject_id=build.id,
        ttl=service.BUILD_WALL_S + 600,
    )
    return {
        "build_id": str(build.id),
        "project_id": str(build.project_id),
        "sandbox": sandbox,
        "env": {},
        "egress": {"allow_hosts": [], "allow_ips": []},
        "limits": service.BUILD_LIMITS,
        "max_wall_s": service.BUILD_WALL_S,
        "uploads": await service.build_upload_urls(build, service.BUILD_WALL_S + 600),
    }


async def _build_done(session: AsyncSession, jb: ExecutionJob, body: dict[str, Any]) -> None:
    build = await session.get(SandboxBuild, uuid.UUID(jb.spec["build_id"]), with_for_update=True)
    if build is None:
        return
    await service.register_build_uploads(session, build, body.get("artifacts") or [])
    service.finish_build(build, body)


runners.register_job_type("sandbox_build", runners.JobType(_build_spec, _build_done))
