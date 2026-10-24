"""Run lifecycle: creation through admission, the guarded state machine, cancellation,
events and completion (FR-14..19, low-level-design.md §4)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.admission.service import RunRequest, admit
from reliability.modules.approvals import service as approvals
from reliability.modules.approvals.models import Approval
from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.loadprofiles import service as profiles
from reliability.modules.loadprofiles.compiler import compile_options
from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.projects.models import Project
from reliability.modules.quotas import service as quotas
from reliability.modules.runs.models import ACTIVE_STATUSES, TERMINAL_STATUSES, Run, RunEvent, RunnerLease
from reliability.modules.targets import service as targets
from reliability.modules.testsuites import service as suites
from reliability.platform import outbox
from reliability.platform.actor import SYSTEM, Actor
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.redis_client import get_redis

log = logging.getLogger(__name__)

ALLOWED: dict[str, set[str]] = {
    "pending_approval": {"queued", "rejected", "cancelled", "invalid"},
    "queued": {"provisioning", "cancelled", "errored", "invalid"},  # invalid: environment cannot be provided
    "provisioning": {"running", "cancelling", "errored", "analyzing"},
    "running": {"analyzing", "cancelling", "errored"},
    "cancelling": {"cancelled", "analyzing", "errored"},
    "analyzing": {"passed", "failed", "invalid", "errored"},
}
SMOKE_OPTIONS_VUS = 1
WALL_CLOCK_SLACK_S = 300


async def transition(session: AsyncSession, run: Run, to: str, reason: str | None = None) -> bool:
    """Guarded state change; returns False if another worker already moved the run."""
    if to not in ALLOWED.get(run.status, set()):
        raise AppError("INVALID_STATE", f"run cannot go from {run.status} to {to}")
    values: dict[str, Any] = {"status": to}
    if reason is not None:
        values["status_reason"] = reason
    now = datetime.now(UTC)
    if to == "queued":
        values["queued_at"] = now
    if to == "running":
        values["started_at"] = now
    if to in TERMINAL_STATUSES:
        values["finished_at"] = now
    result = await session.execute(
        update(Run)
        .where(Run.id == run.id, Run.status == run.status)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return False
    old = run.status
    for k, v in values.items():
        setattr(run, k, v)
    await add_event(session, run, "status", {"status": to, "from": old, "reason": reason})
    return True


async def add_event(session: AsyncSession, run: Run, type_: str, payload: dict[str, Any]) -> int:
    seq = (
        int(
            await session.scalar(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == run.id)) or 0
        )
        + 1
    )
    session.add(
        RunEvent(run_id=run.id, seq=seq, org_id=run.org_id, type=type_, payload=payload, created_at=datetime.now(UTC))
    )
    await session.flush()
    try:
        await get_redis().publish(
            f"run:{run.id}", json.dumps({"seq": seq, "type": type_, "payload": payload}, default=str)
        )
    except Exception:  # live view is best-effort; Postgres has the event
        log.debug("redis publish failed", exc_info=True)
    return seq


SANDBOX_SETUP_S = 900  # build + start + migrate/seed before testing


def _wall_clock(profile: LoadProfile | None) -> int:
    return (profile.duration_s if profile else 600) + WALL_CLOCK_SLACK_S


async def _config(session: AsyncSession, kind: str, suite_id: uuid.UUID, profile: LoadProfile | None) -> dict[str, Any]:
    suite = await suites.get(session, suite_id)
    runnable = await suites.runnable(session, suite)
    base = {"suite_id": str(suite.id), "suite_version": suite.version, "secret_names": runnable["secret_names"]}
    if kind == "load":
        assert profile is not None
        mix = profile.workload_mix or {k: 1.0 for k in runnable["exec_names"]}
        options = compile_options(
            executor=profile.executor,
            stages=profile.stages,
            max_vus=profile.max_vus,
            workload_mix=mix,
            exec_names=runnable["exec_names"],
            endpoints=runnable["endpoints"],
            thresholds=profile.thresholds,
        )
        return {
            **base,
            "k6": {"script": "scenarios.js", "options": options},
            "think_time_s": [float(profile.think_time_min_s), float(profile.think_time_max_s)],
            "abort": {"error_rate": float(profile.thresholds.get("abort_error_rate", 0.5)), "window_s": 30},
            "profile": {
                "type": profile.type,
                "max_vus": profile.max_vus,
                "duration_s": profile.duration_s,
                "thresholds": profile.thresholds,
                "stages": profile.stages,
                "load_profile_id": str(profile.id),
            },
            "max_wall_s": _wall_clock(profile),
        }
    if kind == "functional":
        smoke = None
        if runnable["exec_names"]:
            smoke = {
                "scenarios": {
                    name: {
                        "executor": "per-vu-iterations",
                        "vus": SMOKE_OPTIONS_VUS,
                        "iterations": 1,
                        "exec": name,
                        "maxDuration": "120s",
                    }
                    for name in runnable["exec_names"].values()
                },
                "summaryTrendStats": ["avg", "med", "p(95)", "max", "count"],
                "thresholds": {f"http_req_failed{{workflow:{k}}}": ["rate>=0"] for k in runnable["exec_names"]},
            }
        if not runnable["playwright_specs"] and smoke is None:
            raise AppError("CONFLICT", "the suite has no runnable tests")
        return {
            **base,
            "playwright": {"specs": runnable["playwright_specs"]},
            "k6_smoke": smoke,
            "think_time_s": [0, 0.2],
            "max_wall_s": 1800,
        }
    if kind == "probe":
        if not runnable["probes"]:
            raise AppError("CONFLICT", "the suite has no concurrency probes (add invariants to workflows)")
        return {**base, "probes": runnable["probes"], "max_wall_s": 600}
    raise AppError("VALIDATION_FAILED", f"unsupported run kind {kind}")


async def create_run(
    session: AsyncSession,
    principal: Principal | None,
    project: Project,
    *,
    kind: str,
    suite_id: uuid.UUID | None,
    load_profile_id: uuid.UUID | None,
    target_id: uuid.UUID | None,
    environment_kind: str = "target_url",
    build_ref: dict[str, Any] | None = None,
    notes: str | None = None,
    idempotency_key: str | None = None,
    system: bool = False,
    requested_via: str = "ui",
) -> Run:
    suite = await suites.get(session, suite_id) if suite_id else await suites.latest_ready(session, project.id)
    if suite is None or suite.project_id != project.id:
        raise AppError("CONFLICT", "no ready test suite; generate one first")
    profile: LoadProfile | None = None
    if kind == "load":
        if load_profile_id is None:
            raise AppError("VALIDATION_FAILED", "load runs need a load_profile_id")
        profile = await profiles.get(session, load_profile_id)
        if profile.project_id != project.id:
            raise AppError("NOT_FOUND", "load profile not found")
    target = await targets.get(session, target_id) if target_id else None
    if target is not None and target.project_id != project.id:
        raise AppError("NOT_FOUND", "target not found")
    if environment_kind == "sandbox_build":
        from reliability.modules.environments import source as sandbox_source

        if target is not None:
            raise AppError("VALIDATION_FAILED", "sandbox runs build the app themselves; omit target_id")
        ref = build_ref or {}
        build_ref = {
            "status": "pending",
            "source": await sandbox_source.describe(
                session,
                project.id,
                commit_ref=ref.get("commit_ref"),
                archive_artifact_id=uuid.UUID(ref["archive_artifact_id"]) if ref.get("archive_artifact_id") else None,
            ),
        }

    run_id = uuid7()
    admission = await admit(
        session,
        principal,
        RunRequest(
            org_id=project.org_id,
            project_id=project.id,
            kind=kind,
            target=target,
            profile=profile,
            environment_kind=environment_kind,
            run_id=run_id,
            system=system,
        ),
    )
    config = await _config(session, kind, suite.id, profile)
    if environment_kind == "sandbox_build":
        config["max_wall_s"] = int(config.get("max_wall_s", 1800)) + SANDBOX_SETUP_S
        config["sandbox"] = True
    if target is not None:
        config["target"] = {"base_url": target.base_url, "host": target.host, "environment": target.environment}
    run = Run(
        id=run_id,
        org_id=project.org_id,
        project_id=project.id,
        target_id=target.id if target else None,
        suite_id=suite.id,
        load_profile_id=profile.id if profile else None,
        kind=kind,
        environment_kind=environment_kind,
        build_ref=build_ref,
        status=admission.status,
        requested_by=principal.user_id if principal else None,
        requested_via="system"
        if system
        else ("api_key" if principal and principal.kind == "api_key" else requested_via),
        api_key_id=principal.api_key_id if principal else None,
        idempotency_key=idempotency_key,
        notes=notes,
        config_snapshot=config,
        quota_reservation=admission.reservation,
        queued_at=datetime.now(UTC) if admission.status == "queued" else None,
    )
    session.add(run)
    await session.flush()
    actor = principal.actor if principal else SYSTEM
    await add_event(session, run, "status", {"status": run.status, "reasons": admission.approval_reasons})
    if environment_kind == "sandbox_build":
        await outbox.enqueue(
            session, "environments.prepare_run", {"run_id": run.id, "org_id": run.org_id}, org_id=run.org_id
        )
    if admission.status == "pending_approval":
        await approvals.request(
            session,
            org_id=run.org_id,
            subject_type="run",
            subject_id=run.id,
            requested_by=run.requested_by,
            summary={
                "kind": kind,
                "host": target.host if target else None,
                "environment": target.environment if target else environment_kind,
                "profile": profile.type if profile else None,
                "max_vus": profile.max_vus if profile else None,
                "duration_s": profile.duration_s if profile else None,
                "reasons": admission.approval_reasons,
            },
        )
    await audit.record(
        session,
        actor,
        "run.requested",
        org_id=run.org_id,
        subject_type="run",
        subject_id=run.id,
        metadata={
            "kind": kind,
            "status": run.status,
            "host": target.host if target else None,
            "max_vus": profile.max_vus if profile else None,
        },
    )
    return run


async def _release_quota(session: AsyncSession, run: Run, actual: dict[str, int] | None = None) -> None:
    for kind, reserved in (run.quota_reservation or {}).items():
        await quotas.settle(
            session,
            run.org_id,
            cast(quotas.UsageKind, kind),
            reserved=int(reserved),
            actual=int((actual or {}).get(kind, 0)),
            subject_id=run.id,
        )
    run.quota_reservation = {}


async def on_approval(session: AsyncSession, principal: Principal, approval: Approval) -> None:
    run = await session.get(Run, approval.subject_id, with_for_update=True)
    if run is None or run.status != "pending_approval":
        return
    if approval.decision == "approved":
        await transition(session, run, "queued", f"approved by {principal.actor.id}")
    else:
        await transition(session, run, "rejected", approval.reason or "rejected")
        await _release_quota(session, run)


approvals.register_handler("run", on_approval)


async def get(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise AppError("NOT_FOUND", "run not found")
    return run


async def cancel(session: AsyncSession, actor: Actor, run: Run, reason: str) -> Run:
    await session.refresh(run, with_for_update=True)
    if run.status in ("pending_approval", "queued"):
        await transition(session, run, "cancelled", reason)
        await _release_quota(session, run)
    elif run.status in ("provisioning", "running"):
        await transition(session, run, "cancelling", reason)
        await session.execute(
            update(RunnerLease)
            .where(RunnerLease.subject_id == run.id, RunnerLease.state == "active")
            .values(cancel_requested=True)
        )
    elif run.status == "cancelling":
        pass
    else:
        raise AppError("INVALID_STATE", f"run is already {run.status}")
    await audit.record(
        session,
        actor,
        "run.cancel_requested",
        org_id=run.org_id,
        subject_type="run",
        subject_id=run.id,
        metadata={"reason": reason},
    )
    return run


async def cancel_runs_for_target(target_id: str | uuid.UUID, reason: str) -> int:
    count = 0
    async with system_tx() as session:
        runs = list(
            (
                await session.execute(
                    select(Run).where(
                        Run.target_id == uuid.UUID(str(target_id)),
                        Run.status.in_(("pending_approval", *ACTIVE_STATUSES)),
                    )
                )
            ).scalars()
        )
        for run in runs:
            await cancel(session, SYSTEM, run, reason)
            count += 1
    return count


async def list_for(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> list[Run]:
    stmt = select(Run).where(Run.project_id == project_id).order_by(Run.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(Run.kind == kind)
    if status:
        stmt = stmt.where(Run.status == status)
    if before:
        stmt = stmt.where(Run.created_at < before)
    return list((await session.execute(stmt)).scalars())


async def events_since(
    session: AsyncSession, run_id: uuid.UUID, after_seq: int = 0, limit: int = 500
) -> list[RunEvent]:
    return list(
        (
            await session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                .order_by(RunEvent.seq)
                .limit(limit)
            )
        ).scalars()
    )


async def finish_execution(
    session: AsyncSession,
    run: Run,
    *,
    outcome: str,
    abort_reason: str | None,
    generator_health: dict[str, Any],
    usage: dict[str, int],
    sandbox_report: dict[str, Any] | None = None,
) -> None:
    """Called when the runner completes the lease; analysis happens in a background job."""
    run.generator_health = generator_health
    if sandbox_report is not None:
        run.build_ref = {**(run.build_ref or {}), "report": sandbox_report}
        if outcome == "error" and sandbox_report.get("error"):
            abort_reason = f"sandbox: {sandbox_report['error']}"
    run.config_snapshot = {
        **run.config_snapshot,
        "_execution": {"outcome": outcome, "abort_reason": abort_reason, "usage": usage},
    }
    if run.status == "cancelling":
        await _release_quota(session, run, usage)
        await transition(session, run, "cancelled", run.status_reason or "cancelled")
        return
    if outcome == "error":
        await _release_quota(session, run)
        await transition(session, run, "errored", abort_reason or "execution error")
        return
    await transition(session, run, "analyzing")
    await outbox.enqueue(session, "runs.analyze", {"run_id": str(run.id), "org_id": str(run.org_id)}, org_id=run.org_id)


async def reject_environment(session: AsyncSession, run: Run, reason: str) -> None:
    """The run's environment cannot be provided (e.g. the sandbox cannot be built)."""
    await _release_quota(session, run)
    await transition(session, run, "invalid", reason[:500])


async def settle_usage(session: AsyncSession, run: Run) -> None:
    usage = (run.config_snapshot.get("_execution") or {}).get("usage") or {}
    await _release_quota(session, run, usage)


async def lock_for_update(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = (await session.execute(select(Run).where(Run.id == run_id).with_for_update())).scalar_one_or_none()
    if run is None:
        raise AppError("NOT_FOUND", "run not found")
    return run


async def raw_count_active(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            text("SELECT count(*) FROM runs WHERE status IN ('queued','provisioning','running','cancelling')")
        )
        or 0
    )
