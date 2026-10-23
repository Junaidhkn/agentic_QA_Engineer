"""Project summaries and trends (FR-28). Exports are added in Phase 9."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.findings.models import Finding
from reliability.modules.findings.service import ACTIVE
from reliability.modules.runs.models import Run, RunMetricSummary
from reliability.modules.workflows.service import status_counts

TREND_METRICS = ("p50_ms", "p95_ms", "p99_ms", "error_rate", "rps")


async def project_summary(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for kind in ("functional", "load", "probe"):
        run = (
            await session.execute(
                select(Run)
                .where(Run.project_id == project_id, Run.kind == kind)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run:
            latest[kind] = {
                "id": str(run.id),
                "status": run.status,
                "finished_at": run.finished_at,
                "steady_state": (run.summary or {}).get("steady_state"),
                "knee": (run.summary or {}).get("knee"),
            }
    sev = (
        await session.execute(
            select(Finding.severity, func.count())
            .where(Finding.project_id == project_id, Finding.status.in_(ACTIVE))
            .group_by(Finding.severity)
        )
    ).all()
    capacity = None
    stress = (
        await session.execute(
            select(Run)
            .where(Run.project_id == project_id, Run.kind == "load", Run.status.in_(("passed", "failed")))
            .order_by(Run.created_at.desc())
            .limit(20)
        )
    ).scalars()
    for r in stress:
        knee = (r.summary or {}).get("knee")
        if knee:
            capacity = {"run_id": str(r.id), **knee}
            break
    counts = await status_counts(session, project_id)
    discovered = sum(n for source, by in counts.items() if source != "manual" for n in by.values())
    approved = sum(by.get("approved", 0) for by in counts.values())
    return {
        "latest_runs": latest,
        "open_findings": {s: n for s, n in sev},
        "capacity": capacity,
        "workflows": {"discovered": discovered, "approved": approved, "by_source": counts},
    }


async def trends(
    session: AsyncSession, project_id: uuid.UUID, scope: str, metric: str, limit: int = 30
) -> list[dict[str, Any]]:
    if metric not in TREND_METRICS:
        raise ValueError("unsupported metric")
    rows = (
        await session.execute(
            select(Run.id, Run.created_at, Run.status, Run.load_profile_id, RunMetricSummary)
            .join(RunMetricSummary, RunMetricSummary.run_id == Run.id)
            .where(
                Run.project_id == project_id,
                Run.kind == "load",
                Run.status.in_(("passed", "failed")),
                RunMetricSummary.scope == scope,
                RunMetricSummary.time_window == "steady_state",
            )
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "run_id": str(r[0]),
            "at": r[1].isoformat(),
            "status": r[2],
            "load_profile_id": str(r[3]) if r[3] else None,
            "value": float(getattr(r[4], metric)) if getattr(r[4], metric) is not None else None,
        }
        for r in reversed(rows)
    ]
