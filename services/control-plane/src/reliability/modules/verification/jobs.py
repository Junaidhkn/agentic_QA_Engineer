"""Verification jobs: advance the gate pipeline when a check job or run finishes, and measure the
per-project noise floor with A/A runs (weekly, or on request).

A/A measurement: six sandbox load runs of the same build in sequence (A B A B A B), compared
with the same statistics as the ``load_comparison`` gate; the CI half-width becomes the noise
floor used by later comparisons (evaluation.md §6).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.projects.models import Project
from reliability.modules.runs import jobs as run_jobs
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import TERMINAL_STATUSES, Run
from reliability.modules.verification import service
from reliability.platform import outbox
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.jobs import job
from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)
run_jobs.POST_ANALYSIS_TOPICS.append("verification.run_done")
AA_RUNS = 6


@job("verification.advance")
async def advance(payload: dict[str, Any]) -> None:
    org_id, vid = uuid.UUID(payload["org_id"]), uuid.UUID(payload["verification_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        await service.advance(s, vid)


@job("verification.run_done")
async def run_done(payload: dict[str, Any]) -> None:
    org_id, run_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["run_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(Run, run_id)
        if run is None:
            return
        cfg = run.config_snapshot or {}
        if (ver := cfg.get("verification")) is not None:
            await service.advance(s, uuid.UUID(ver["verification_id"]))
        elif (aa := cfg.get("noise_floor")) is not None:
            await _noise_floor_step(s, run, aa)


# --------------------------------------------------------- noise floor ----
async def start_noise_floor(
    session: AsyncSession,
    project: Project,
    *,
    archive_artifact_id: str,
    suite_id: uuid.UUID,
    workflow_keys: list[str],
    vus: int,
) -> uuid.UUID:
    group = uuid7()
    await _aa_run(session, project, archive_artifact_id, suite_id, workflow_keys, vus, group, 0)
    return group


async def _aa_run(
    session: AsyncSession,
    project: Project,
    archive: str,
    suite_id: uuid.UUID,
    workflow_keys: list[str],
    vus: int,
    group: uuid.UUID,
    index: int,
) -> Run:
    s = get_settings()
    profile = LoadProfile(
        id=uuid7(),
        org_id=project.org_id,
        project_id=project.id,
        name="noise floor (A/A)",
        type="load",
        executor="ramping-vus",
        stages=[
            {"duration_s": service.RAMP_S, "target": vus},
            {"duration_s": s.verification_load_hold_s, "target": vus},
        ],
        max_vus=vus,
        duration_s=service.RAMP_S + s.verification_load_hold_s,
        think_time_min_s=Decimal("0"),
        think_time_max_s=Decimal("0.05"),
        workload_mix={k: 1.0 for k in workflow_keys},
        thresholds={"p95_ms": 100000, "error_rate": 1.0, "abort_error_rate": 1.0},
    )
    return await runs.create_run(
        session,
        None,
        project,
        kind="load",
        suite_id=suite_id,
        load_profile_id=None,
        target_id=None,
        environment_kind="sandbox_build",
        build_ref={"archive_artifact_id": archive},
        system=True,
        requested_via="system",
        profile_override=profile,
        config_extra={
            "noise_floor": {
                "group": str(group),
                "index": index,
                "archive": archive,
                "workflow_keys": workflow_keys,
                "vus": vus,
            }
        },
        notes=f"noise floor A/A run {index + 1}/{AA_RUNS}",
    )


async def _noise_floor_step(session: AsyncSession, run: Run, aa: dict[str, Any]) -> None:
    group, index = str(aa["group"]), int(aa["index"])
    if run.status not in ("passed", "failed"):
        log.warning("noise floor run %s ended %s; measurement abandoned", run.id, run.status)
        return
    project = await session.get(Project, run.project_id)
    if project is None or run.suite_id is None:
        return
    if index + 1 < AA_RUNS:
        await _aa_run(
            session,
            project,
            aa["archive"],
            run.suite_id,
            list(aa["workflow_keys"]),
            int(aa["vus"]),
            uuid.UUID(group),
            index + 1,
        )
        return
    group_runs = [
        r
        for r in (
            await session.execute(
                select(Run).where(
                    Run.project_id == run.project_id, Run.config_snapshot["noise_floor"]["group"].astext == group
                )
            )
        ).scalars()
        if r.status in TERMINAL_STATUSES
    ]
    group_runs.sort(key=lambda r: int(r.config_snapshot["noise_floor"]["index"]))
    a = [r for r in group_runs if int(r.config_snapshot["noise_floor"]["index"]) % 2 == 0]
    b = [r for r in group_runs if int(r.config_snapshot["noise_floor"]["index"]) % 2 == 1]
    try:
        results = await service.compare_runs(session, a, b, ["overall"], service.stats.DEFAULT_NOISE_FLOOR_PCT)
    except AppError as exc:
        log.warning("noise floor comparison failed: %s", exc.message)
        return
    for scope, c in results.items():
        session.add(service._comparison_row(run.org_id, run.project_id, None, a, b, scope, c))


@job("verification.noise_floor_sweep")
async def noise_floor_sweep(payload: dict[str, Any]) -> None:
    """Weekly: re-measure the noise floor for projects that ran sandbox load tests last week."""
    for org_id, run_id in await runs.latest_sandbox_load_runs(datetime.now(UTC) - timedelta(days=7)):
        await outbox_noise_floor(org_id, run_id)


async def outbox_noise_floor(org_id: uuid.UUID, template_run_id: uuid.UUID) -> None:
    async with tenant_tx(org_ids=[org_id]) as s:
        await outbox.enqueue(
            s,
            "verification.noise_floor_from_run",
            {"run_id": str(template_run_id), "org_id": str(org_id)},
            org_id=org_id,
        )


@job("verification.noise_floor_from_run")
async def noise_floor_from_run(payload: dict[str, Any]) -> None:
    """Start an A/A measurement that reuses a finished sandbox load run's build and workload."""
    org_id, run_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["run_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(Run, run_id)
        project = await s.get(Project, run.project_id) if run else None
        if run is None or project is None or run.suite_id is None:
            return
        archive = (run.build_ref or {}).get("archive_artifact_id")
        mix = ((run.config_snapshot.get("profile") or {}).get("workload_mix")) or {}
        if not archive or not mix:
            return
        vus = int((run.config_snapshot.get("profile") or {}).get("max_vus") or 10)
        await start_noise_floor(
            s, project, archive_artifact_id=archive, suite_id=run.suite_id, workflow_keys=sorted(mix), vus=vus
        )
