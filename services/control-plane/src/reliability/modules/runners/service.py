"""Lease protocol between the control plane and execution-plane runners (ADR-0008, api-contract §7).

Runners pull work; a lease grants exactly one job for a short TTL that heartbeats extend.
Cancellation (user stop, kill switch, revoked target) is delivered as the heartbeat reply.
Lost leases are reaped and their runs marked ``errored`` — load runs are never retried
automatically so no surprise traffic reaches a target.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.runners.spec import build_run_spec, upload_key
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import ExecutionJob, Run, RunnerLease
from reliability.modules.targets.models import Target
from reliability.modules.tenancy.models import Organization
from reliability.platform import metrics
from reliability.platform.actor import SYSTEM, Actor
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.flags import kill_switch_active
from reliability.platform.ids import uuid7
from reliability.platform.jobs import load_all_jobs
from reliability.platform.settings import get_settings
from reliability.platform.storage import get_store

log = logging.getLogger(__name__)
RUN_JOB_TYPES = ("functional", "load", "probe", "verification")


# --------------------------------------------- generic execution jobs ----
@dataclass(frozen=True)
class JobType:
    build_spec: Callable[[AsyncSession, ExecutionJob, str, str, str], Awaitable[dict[str, Any]]]
    on_complete: Callable[[AsyncSession, ExecutionJob, dict[str, Any]], Awaitable[None]]


_JOB_TYPES: dict[str, JobType] = {}


def register_job_type(name: str, job_type: JobType) -> None:
    _JOB_TYPES[name] = job_type


async def enqueue_job(
    session: AsyncSession, *, org_id: uuid.UUID, job_type: str, subject_id: uuid.UUID, spec: dict[str, Any]
) -> ExecutionJob:
    job = ExecutionJob(id=uuid7(), org_id=org_id, job_type=job_type, subject_id=subject_id, spec=spec, status="queued")
    session.add(job)
    await session.flush()
    return job


# ---------------------------------------------------------------- lease ----
def _ttl() -> timedelta:
    return timedelta(seconds=get_settings().runner_lease_ttl_seconds)


async def _try_acquire(runner: str, public_key: str, job_types: set[str]) -> dict[str, Any] | None:
    async with system_tx() as session:
        if await kill_switch_active(session, None):
            return None
        if job_types & set(RUN_JOB_TYPES):
            run = (
                await session.execute(
                    select(Run)
                    .where(
                        Run.status == "queued",
                        Run.kind.in_(job_types & set(RUN_JOB_TYPES)),
                        # Sandbox runs need a sandbox-capable runner and a prepared build plan.
                        or_(
                            Run.environment_kind == "target_url",
                            and_(
                                literal("sandbox_env" in job_types),
                                Run.build_ref["status"].astext == "ready",
                            ),
                        ),
                    )
                    .order_by(Run.queued_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if run is not None:
                return await _lease_run(session, run, runner, public_key)
        other = job_types - set(RUN_JOB_TYPES) - {"sandbox_env"}
        if other:
            job = (
                await session.execute(
                    select(ExecutionJob)
                    .where(ExecutionJob.status == "queued", ExecutionJob.job_type.in_(other))
                    .order_by(ExecutionJob.queued_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if job is not None:
                return await _lease_job(session, job, runner, public_key)
    return None


async def _lease_run(session: AsyncSession, run: Run, runner: str, public_key: str) -> dict[str, Any] | None:
    org = await session.get(Organization, run.org_id)
    target = await session.get(Target, run.target_id) if run.target_id else None
    problem = None
    if org is None or org.suspended_at is not None:
        problem = "organization suspended"
    elif await kill_switch_active(session, run.org_id):
        problem = "execution paused by operator"
    elif run.environment_kind == "target_url" and (target is None or not target.is_runnable):
        problem = "target is no longer verified"
    if problem:
        await runs.transition(session, run, "errored", problem)
        await runs.settle_usage(session, run)
        return None
    lease_id = uuid7()
    spec = await build_run_spec(session, run, target, str(lease_id), runner, public_key)
    session.add(
        RunnerLease(
            id=lease_id,
            org_id=run.org_id,
            job_type=run.kind,
            subject_id=run.id,
            runner_id=runner,
            state="active",
            spec={"uploads": list(spec["uploads"])},
            expires_at=datetime.now(UTC) + _ttl(),
        )
    )
    await runs.transition(session, run, "provisioning", f"leased by {runner}")
    if run.environment_kind == "sandbox_build" and run.queued_at is not None:
        metrics.observe_job_start(f"{run.kind}_sandbox", (datetime.now(UTC) - run.queued_at).total_seconds())
    await audit.record(
        session,
        Actor("runner", runner),
        "run.leased",
        org_id=run.org_id,
        subject_type="run",
        subject_id=run.id,
        metadata={"lease_id": str(lease_id)},
    )
    return spec


async def _lease_job(session: AsyncSession, job: ExecutionJob, runner: str, public_key: str) -> dict[str, Any] | None:
    load_all_jobs()  # job types register on import of their module's ``jobs``
    job_type = _JOB_TYPES.get(job.job_type)
    if job_type is None:
        return None
    lease_id = uuid7()
    try:
        spec = await job_type.build_spec(session, job, str(lease_id), runner, public_key)
    except AppError as exc:  # e.g. target no longer verified: fail the job instead of retrying forever
        job.status, job.finished_at = "failed", datetime.now(UTC)
        job.result = {"outcome": "error", "abort_reason": exc.message}
        await job_type.on_complete(session, job, job.result)
        return None
    spec.update(
        {
            "lease_id": str(lease_id),
            "job_type": job.job_type,
            "job_id": str(job.id),
            "org_id": str(job.org_id),
            "heartbeat_interval_s": spec.get("heartbeat_interval_s", 3),
        }
    )
    session.add(
        RunnerLease(
            id=lease_id,
            org_id=job.org_id,
            job_type=job.job_type,
            subject_id=job.id,
            runner_id=runner,
            state="active",
            spec={"uploads": list(spec.get("uploads", {}))},
            expires_at=datetime.now(UTC) + _ttl(),
        )
    )
    job.status = "leased"
    metrics.observe_job_start(job.job_type, (datetime.now(UTC) - job.queued_at).total_seconds())
    return spec


async def acquire(
    runner: str, public_key: str, job_types: list[str], wait_seconds: float | None = None
) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + (
        get_settings().runner_long_poll_seconds if wait_seconds is None else wait_seconds
    )
    types = set(job_types)
    while True:
        lease = await _try_acquire(runner, public_key, types)
        if lease is not None or asyncio.get_running_loop().time() >= deadline:
            return lease
        await asyncio.sleep(0.5)


async def _active_lease(session: AsyncSession, lease_id: uuid.UUID, runner: str) -> RunnerLease:
    lease = await session.get(RunnerLease, lease_id, with_for_update=True)
    if lease is None or lease.runner_id != runner:
        raise AppError("NOT_FOUND", "lease not found")
    if lease.state != "active":
        raise AppError("INVALID_STATE", f"lease is {lease.state}")
    return lease


async def heartbeat(
    runner: str, lease_id: uuid.UUID, phase: str | None, progress: dict[str, Any] | None
) -> dict[str, Any]:
    async with system_tx() as session:
        lease = await _active_lease(session, lease_id, runner)
        lease.expires_at = datetime.now(UTC) + _ttl()
        lease.last_heartbeat_at = datetime.now(UTC)
        cancel = lease.cancel_requested or await kill_switch_active(session, lease.org_id)
        if lease.job_type in RUN_JOB_TYPES:
            run = await session.get(Run, lease.subject_id, with_for_update=True)
            assert run is not None
            if run.status == "cancelling":
                cancel = True
            if run.target_id:
                target = await session.get(Target, run.target_id)
                if target is None or target.status != "verified":
                    cancel = True
                    if run.status in ("provisioning", "running"):
                        await runs.transition(session, run, "cancelling", "target verification revoked")
            if phase == "running" and run.status == "provisioning":
                await runs.transition(session, run, "running")
            if progress:
                await runs.add_event(session, run, "progress", progress)
        return {"action": "cancel" if cancel else "continue", "extend_to": lease.expires_at.isoformat()}


async def post_events(runner: str, lease_id: uuid.UUID, events: list[dict[str, Any]]) -> int:
    async with system_tx() as session:
        lease = await _active_lease(session, lease_id, runner)
        if lease.job_type not in RUN_JOB_TYPES:
            return 0
        run = await session.get(Run, lease.subject_id)
        assert run is not None
        for event in events[:200]:
            await runs.add_event(session, run, str(event.get("type", "log"))[:40], event.get("payload") or {})
        return len(events[:200])


async def _register_uploads(
    session: AsyncSession, run: Run, lease: RunnerLease, reported: list[dict[str, Any]]
) -> None:
    allowed = set(lease.spec.get("uploads", []))
    store = get_store()
    for item in reported:
        name = item.get("name", "")
        if name not in allowed:
            continue
        key = upload_key(run, name)
        if not store.exists(key):
            continue
        sha = bytes.fromhex(item.get("sha256", "")) if item.get("sha256") else b""
        await artifacts.register(
            session,
            org_id=run.org_id,
            project_id=run.project_id,
            kind=name.split(":", 1)[0],
            key=key,
            size=int(item.get("size", 0)),
            sha256=sha,
            content_type=item.get("content_type", "application/octet-stream"),
            run_id=run.id,
        )


async def complete(runner: str, lease_id: uuid.UUID, body: dict[str, Any]) -> None:
    async with system_tx() as session:
        lease = await _active_lease(session, lease_id, runner)
        lease.state = "completed" if body.get("outcome") != "error" else "failed"
        lease.finished_at = datetime.now(UTC)
        kill = (body.get("generator_health") or {}).get("kill_latency_s")
        if isinstance(kill, int | float):
            metrics.KILL_LATENCY.labels(lease.job_type).observe(float(kill))
        if lease.job_type in RUN_JOB_TYPES:
            run = await session.get(Run, lease.subject_id, with_for_update=True)
            assert run is not None
            await _register_uploads(session, run, lease, body.get("artifacts", []))
            await runs.finish_execution(
                session,
                run,
                outcome=str(body.get("outcome", "succeeded")),
                abort_reason=body.get("abort_reason"),
                generator_health=body.get("generator_health") or {},
                usage={k: int(v) for k, v in (body.get("usage") or {}).items()},
                sandbox_report=(body.get("result") or {}).get("sandbox"),
            )
            await audit.record(
                session,
                Actor("runner", runner),
                "run.executed",
                org_id=run.org_id,
                subject_type="run",
                subject_id=run.id,
                metadata={"outcome": body.get("outcome"), "abort_reason": body.get("abort_reason")},
            )
            return
        job = await session.get(ExecutionJob, lease.subject_id, with_for_update=True)
        assert job is not None
        job.status = "completed" if body.get("outcome") != "error" else "failed"
        job.result, job.finished_at = body, datetime.now(UTC)
        job_type = _JOB_TYPES.get(job.job_type)
        if job_type:
            await job_type.on_complete(session, job, body)


async def reap_expired() -> int:
    """Mark leases with missed heartbeats as expired and error their runs (lease TTL 15 s)."""
    reaped = 0
    async with system_tx() as session:
        leases = list(
            (
                await session.execute(
                    select(RunnerLease)
                    .where(RunnerLease.state == "active", RunnerLease.expires_at < datetime.now(UTC))
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).scalars()
        )
        for lease in leases:
            lease.state, lease.finished_at = "expired", datetime.now(UTC)
            reaped += 1
            metrics.LEASES_EXPIRED.labels(lease.job_type).inc()
            if lease.job_type in RUN_JOB_TYPES:
                run = await session.get(Run, lease.subject_id, with_for_update=True)
                if run is not None and run.status in ("provisioning", "running", "cancelling"):
                    target = "cancelled" if run.status == "cancelling" else "errored"
                    await runs.transition(session, run, target, "runner lost (missed heartbeats)")
                    await runs.settle_usage(session, run)
                    await audit.record(
                        session,
                        SYSTEM,
                        "run.runner_lost",
                        org_id=run.org_id,
                        subject_type="run",
                        subject_id=run.id,
                        metadata={"runner_id": lease.runner_id},
                    )
            else:
                await session.execute(
                    update(ExecutionJob)
                    .where(ExecutionJob.id == lease.subject_id)
                    .values(status="failed", result={"error": "runner lost"}, finished_at=datetime.now(UTC))
                )
    return reaped


async def request_cancel_all(org_id: uuid.UUID | None) -> int:
    """Kill switch support: flag every active lease (optionally for one org) for cancellation."""
    async with system_tx() as session:
        stmt = update(RunnerLease).where(RunnerLease.state == "active")
        if org_id:
            stmt = stmt.where(RunnerLease.org_id == org_id)
        result = await session.execute(stmt.values(cancel_requested=True))
        return int(result.rowcount)  # type: ignore[attr-defined]
