"""Synthetic canary (operations §6): every 15 minutes a dedicated canary project runs its
functional suite and a small load run against the reference app, exercising the whole path —
admission → outbox → lease → runner → k6/Playwright → analysis → findings.

Each firing first judges the previous pair: both passed → ``ok``; any failed/errored/invalid,
or still unfinished after ``canary_max_age_s`` → ``failed``. Then it starts the next pair.
The SLO rules alert on ``re_platform_check_*{check="canary"}``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.ops import checks
from reliability.modules.projects.models import Project
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import Run
from reliability.platform.db import system_tx, tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)
TERMINAL_OK = {"passed"}
TERMINAL_BAD = {"failed", "errored", "invalid", "cancelled", "rejected"}
LOAD_VUS = 3
LOAD_HOLD_S = 30


def _profile(project: Project) -> LoadProfile:
    return LoadProfile(
        id=uuid.uuid4(),
        org_id=project.org_id,
        project_id=project.id,
        name="canary",
        type="load",
        executor="ramping-vus",
        stages=[{"duration_s": 10, "target": LOAD_VUS}, {"duration_s": LOAD_HOLD_S, "target": LOAD_VUS}],
        max_vus=LOAD_VUS,
        duration_s=10 + LOAD_HOLD_S,
        think_time_min_s=Decimal("0.5"),
        think_time_max_s=Decimal("1"),
        workload_mix={},
        thresholds={"p95_ms": 2000, "error_rate": 0.05, "abort_error_rate": 0.5},
    )


async def judge(previous: dict[str, Any], now: datetime) -> tuple[bool | None, dict[str, Any]]:
    """``None`` while the previous pair is still within its time limit."""
    ids = [uuid.UUID(i) for i in previous.get("run_ids", [])]
    if not ids:
        return None, {}
    started = datetime.fromisoformat(previous["started_at"])
    async with system_tx() as s:
        statuses = {str(i): (r.status if (r := await s.get(Run, i)) else "missing") for i in ids}
    outcome = {"previous_runs": statuses}
    if any(st in TERMINAL_BAD or st == "missing" for st in statuses.values()):
        return False, {**outcome, "reason": "a canary run did not pass"}
    if all(st in TERMINAL_OK for st in statuses.values()):
        return True, {**outcome, "duration_s": round((now - started).total_seconds(), 1)}
    if (now - started).total_seconds() > get_settings().canary_max_age_s:
        return False, {**outcome, "reason": "canary runs did not finish in time"}
    return None, outcome


async def start_pair(project_id: uuid.UUID, target_id: uuid.UUID) -> list[str]:
    async with system_tx() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise AppError("NOT_FOUND", "canary project not found")
        org_id = project.org_id
    ids = []
    async with tenant_tx(org_ids=[org_id]) as s:
        project = await s.get(Project, project_id)
        assert project is not None
        for kind in ("functional", "load"):
            run = await runs.create_run(
                s,
                None,
                project,
                kind=kind,
                suite_id=None,
                load_profile_id=None,
                target_id=target_id,
                system=True,
                requested_via="system",
                profile_override=_profile(project) if kind == "load" else None,
                config_extra={"canary": True},
                notes="synthetic canary",
            )
            ids.append(str(run.id))
    return ids


async def run(now: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    now = now or datetime.now(UTC)
    if settings.canary_project_id is None or settings.canary_target_id is None:
        if settings.env in ("staging", "production"):
            async with system_tx() as s:
                await checks.record(s, checks.CANARY, False, {"reason": "RE_CANARY_PROJECT_ID/TARGET_ID not set"})
        return {"configured": False}
    async with system_tx() as s:
        current = await checks.get(s, checks.CANARY)
    previous = dict(current.detail) if current else {}
    verdict, outcome = await judge(previous, now)
    if verdict is None and previous.get("run_ids"):
        return {"waiting": True, **outcome}  # the last pair is still running; judge it next time
    try:
        ids = await start_pair(settings.canary_project_id, settings.canary_target_id)
        detail: dict[str, Any] = {"run_ids": ids, "started_at": now.isoformat(), **outcome}
        ok = verdict if verdict is not None else True
    except AppError as exc:
        log.warning("canary could not start", extra={"code": exc.code})
        detail, ok = {"reason": f"could not start: {exc.code}: {exc.message}", **outcome}, False
    if verdict is False:
        ok = False
    async with system_tx() as s:
        if verdict is None and ok:
            # First firing (nothing judged yet): keep last_success_at untouched, record "running".
            await checks.mark_running(s, checks.CANARY, detail)
        else:
            await checks.record(s, checks.CANARY, ok, detail)
    return {"ok": ok, **detail}
