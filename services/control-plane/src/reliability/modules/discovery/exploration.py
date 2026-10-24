"""``ui_explore`` runner jobs (FR-8): a bounded crawl of a *verified* target before discovery.

The lease carries the target URL, an egress allowlist of exactly that target, the action/time
budget and — sealed for the runner — only the two test-credential secrets the user named. The
crawl result comes back in the job result; discovery then resumes with it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.discovery.models import DiscoveryRun
from reliability.modules.runners import service as runners
from reliability.modules.runs.models import ExecutionJob
from reliability.modules.secrets import service as secrets
from reliability.modules.targets import service as targets
from reliability.platform import outbox
from reliability.platform.actor import Actor
from reliability.platform.crypto import seal_for_x25519
from reliability.platform.errors import AppError

JOB_TYPE = "ui_explore"
LIMITS = {"cpu": 2, "mem_gb": 4, "pids": 1024}


async def enqueue(session: AsyncSession, run: DiscoveryRun) -> ExecutionJob:
    return await runners.enqueue_job(
        session, org_id=run.org_id, job_type=JOB_TYPE, subject_id=run.id, spec={"discovery_id": str(run.id)}
    )


async def _build_spec(
    session: AsyncSession, job: ExecutionJob, lease_id: str, runner_id: str, public_key: str
) -> dict[str, Any]:
    run = await session.get(DiscoveryRun, uuid.UUID(job.spec["discovery_id"]))
    if run is None or run.target_id is None:
        raise AppError("NOT_FOUND", "discovery run not found")
    target = await targets.get(session, run.target_id)
    if not target.is_runnable:
        raise AppError("TARGET_NOT_VERIFIED", "the exploration target is no longer verified")
    opts = run.options.get("exploration") or {}
    names = {n for n in (opts.get("username_secret"), opts.get("password_secret")) if n}
    values = (
        await secrets.resolve_for_run(session, Actor("runner", runner_id), run.org_id, run.project_id, names, run.id)
        if names
        else {}
    )
    seconds = int(opts.get("time_budget_s", 900))
    return {
        "project_id": str(run.project_id),
        "env": {"BASE_URL": target.base_url},
        "secrets_sealed": seal_for_x25519(public_key, json.dumps(values).encode()),
        "egress": {"allow_hosts": [target.host], "allow_ips": [str(ip) for ip in target.resolved_ips or []]},
        "explore": {
            "base_url": target.base_url,
            "start_path": opts.get("start_path") or "/",
            "max_actions": int(opts.get("action_budget", 150)),
            "max_seconds": seconds,
            "max_depth": 6,
            "username_secret": opts.get("username_secret") or "",
            "password_secret": opts.get("password_secret") or "",
        },
        "limits": LIMITS,
        "max_wall_s": seconds + 120,
        "uploads": {},
    }


async def _done(session: AsyncSession, job: ExecutionJob, body: dict[str, Any]) -> None:
    await outbox.enqueue(
        session,
        "discovery.run",
        {"discovery_id": job.spec["discovery_id"], "org_id": str(job.org_id)},
        org_id=job.org_id,
    )


def result_of(job: ExecutionJob | None) -> dict[str, Any] | None:
    """The crawl result, or a stub describing why there is none (discovery continues without it)."""
    if job is None or job.status not in ("completed", "failed"):
        return None
    body = job.result or {}
    explored = (body.get("result") or {}).get("exploration")
    if not explored:
        return {"pages": [], "actions": [], "api_calls": [], "error": body.get("abort_reason") or "exploration failed"}
    return dict(explored)


runners.register_job_type(JOB_TYPE, runners.JobType(_build_spec, _done))
