"""``repair_check`` runner jobs: build and start a sandbox from a source archive, then run a fixed
list of allowlisted commands (regression test, the repo's test command…) and optionally the
security scanners. Used by the repair agent's deterministic check nodes and by the verification
pipeline; the requester names an outbox topic that receives ``{**notify_payload, job_id}`` when
the job finishes, and reads the outcome with :func:`outcome`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.environments import service
from reliability.modules.runners import service as runners
from reliability.modules.runs.models import ExecutionJob
from reliability.platform import outbox
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

JOB_TYPE = "repair_check"
CHECK_LIMITS = {"cpu": 2, "mem_gb": 4, "pids": 2048}


@dataclass(frozen=True)
class Check:
    id: str
    argv: list[str]
    repeat: int = 1

    def to_dict(self, timeout_s: int) -> dict[str, Any]:
        return {"id": self.id, "argv": self.argv, "repeat": self.repeat, "timeout_s": timeout_s}


async def enqueue(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    subject_id: uuid.UUID,
    archive_artifact_id: str,
    plan: dict[str, Any],
    checks: list[Check],
    scan: bool,
    notify_topic: str,
    notify_payload: dict[str, Any],
) -> ExecutionJob:
    timeout = get_settings().repair_check_timeout_s
    return await runners.enqueue_job(
        session,
        org_id=org_id,
        job_type=JOB_TYPE,
        subject_id=subject_id,
        spec={
            "project_id": str(project_id),
            "archive_artifact_id": archive_artifact_id,
            "plan": plan,
            "checks": [c.to_dict(timeout) for c in checks],
            "scan": scan,
            "notify_topic": notify_topic,
            "notify_payload": notify_payload,
            "max_wall_s": service.BUILD_WALL_S + timeout * sum(c.repeat for c in checks),
        },
    )


@dataclass
class Outcome:
    finished: bool
    built: bool  # sandbox built, started and healthy
    error: str | None
    checks: dict[str, list[dict[str, Any]]]  # check id -> attempts
    scan: dict[str, Any] | None
    sandbox: dict[str, Any]

    def exit_codes(self, check_id: str) -> list[int | None]:
        return [a.get("exit_code") for a in self.checks.get(check_id, [])]


def outcome(job: ExecutionJob) -> Outcome:
    if job.status not in ("completed", "failed"):
        return Outcome(False, False, None, {}, None, {})
    body = job.result or {}
    result = body.get("result") or {}
    sandbox = result.get("sandbox") or {}
    checks: dict[str, list[dict[str, Any]]] = {}
    for item in result.get("checks") or []:
        checks.setdefault(str(item.get("id")), []).append(item)
    built = body.get("outcome") == "succeeded" and sandbox.get("healthy") is True
    error = None if built else (sandbox.get("error") or body.get("abort_reason") or body.get("error") or "failed")
    return Outcome(True, built, error, checks, result.get("scan"), sandbox)


async def _build_spec(
    session: AsyncSession, job: ExecutionJob, lease_id: str, runner_id: str, public_key: str
) -> dict[str, Any]:
    spec = job.spec
    if not spec.get("archive_artifact_id") or not spec.get("plan"):
        raise AppError("CONFLICT", "check job has no source")
    wall = int(spec.get("max_wall_s", 1800))
    sandbox = await service.sandbox_lease_section(
        session,
        org_id=job.org_id,
        project_id=uuid.UUID(spec["project_id"]),
        archive_artifact_id=spec["archive_artifact_id"],
        plan=spec["plan"],
        runner_id=runner_id,
        runner_public_key=public_key,
        subject_id=job.id,
        ttl=wall + 600,
    )
    return {
        "project_id": spec["project_id"],
        "sandbox": sandbox,
        "checks": spec["checks"],
        "scan": bool(spec.get("scan")),
        "env": {},
        "egress": {"allow_hosts": [], "allow_ips": []},
        "limits": CHECK_LIMITS,
        "max_wall_s": wall,
        "uploads": {},
    }


async def _done(session: AsyncSession, job: ExecutionJob, body: dict[str, Any]) -> None:
    topic = job.spec.get("notify_topic")
    if topic:
        await outbox.enqueue(
            session, topic, {**(job.spec.get("notify_payload") or {}), "job_id": str(job.id)}, org_id=job.org_id
        )


runners.register_job_type(JOB_TYPE, runners.JobType(_build_spec, _done))
