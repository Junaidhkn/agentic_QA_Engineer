"""Project summaries, trends and report exports with share links (FR-28, UF-7)."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.findings.models import Finding
from reliability.modules.findings.service import ACTIVE
from reliability.modules.identity.service import Principal
from reliability.modules.reports import export
from reliability.modules.reports.models import Report
from reliability.modules.runs.models import Run, RunMetricSummary
from reliability.modules.workflows.service import status_counts
from reliability.platform import outbox
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

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


# ------------------------------------------------------------- exports ----
MAX_PERIOD = timedelta(days=366)
MAX_SHARE_DAYS = 30


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create(
    session: AsyncSession,
    principal: Principal,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    start: datetime | None,
    end: datetime | None,
    sections: list[str] | None,
) -> Report:
    end = end or datetime.now(UTC)
    start = start or end - timedelta(days=30)
    if start >= end or end - start > MAX_PERIOD:
        raise AppError("VALIDATION_FAILED", "the report period must be positive and at most one year")
    unknown = sorted(set(sections or []) - set(export.SECTIONS))
    if unknown:
        raise AppError(
            "VALIDATION_FAILED", f"unknown sections: {', '.join(unknown)}", {"sections": list(export.SECTIONS)}
        )
    if principal.user_id is None:
        raise AppError("FORBIDDEN", "reports are requested by a person")
    report = Report(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        params={"from": start.isoformat(), "to": end.isoformat(), "sections": sections or list(export.SECTIONS)},
        status="queued",
        requested_by=principal.user_id,
    )
    session.add(report)
    await session.flush()
    await outbox.enqueue(
        session, "reports.generate", {"report_id": str(report.id), "org_id": str(org_id)}, org_id=org_id
    )
    return report


async def generate(session: AsyncSession, report: Report, now: datetime | None = None) -> None:
    p = report.params
    doc = await export.build(
        session,
        report.project_id,
        datetime.fromisoformat(p["from"]),
        datetime.fromisoformat(p["to"]),
        p.get("sections"),
        now or datetime.now(UTC),
    )
    data = json.dumps(doc, indent=2, default=str).encode()
    page = export.render_html(doc).encode()
    j = await artifacts.put(
        session,
        org_id=report.org_id,
        project_id=report.project_id,
        kind="report_json",
        owner_id=report.id,
        name="report.json",
        data=data,
        content_type="application/json",
    )
    h = await artifacts.put(
        session,
        org_id=report.org_id,
        project_id=report.project_id,
        kind="report_html",
        owner_id=report.id,
        name="report.html",
        data=page,
        content_type="text/html; charset=utf-8",
    )
    report.json_artifact_id, report.html_artifact_id, report.status = j.id, h.id, "ready"


async def get(session: AsyncSession, report_id: uuid.UUID) -> Report:
    report = await session.get(Report, report_id)
    if report is None:
        raise AppError("NOT_FOUND", "report not found")
    return report


async def for_project(session: AsyncSession, project_id: uuid.UUID, limit: int = 50) -> list[Report]:
    return list(
        (
            await session.execute(
                select(Report).where(Report.project_id == project_id).order_by(Report.created_at.desc()).limit(limit)
            )
        ).scalars()
    )


async def share(session: AsyncSession, principal: Principal, report: Report, days: int) -> tuple[str, datetime]:
    """Create (or replace) the report's share link. The token is returned once and stored hashed."""
    if report.status != "ready":
        raise AppError("INVALID_STATE", "the report is not ready yet")
    days = max(1, min(days, MAX_SHARE_DAYS))
    token = secrets.token_urlsafe(32)
    report.share_token_hash = _hash(token)
    report.share_expires_at = datetime.now(UTC) + timedelta(days=days)
    await audit.record(
        session,
        principal.actor,
        "report.shared",
        org_id=report.org_id,
        subject_type="report",
        subject_id=report.id,
        metadata={"expires_at": report.share_expires_at.isoformat()},
    )
    return token, report.share_expires_at


async def revoke_share(session: AsyncSession, principal: Principal, report: Report) -> None:
    report.share_token_hash, report.share_expires_at = None, None
    await audit.record(
        session,
        principal.actor,
        "report.share_revoked",
        org_id=report.org_id,
        subject_type="report",
        subject_id=report.id,
    )


async def shared_html(token: str) -> bytes:
    """Public share-link lookup by token hash (no tenant context, hence the system role)."""
    if not token or len(token) > 100:
        raise AppError("NOT_FOUND", "link not found")
    async with system_tx() as session:
        report = (
            await session.execute(select(Report).where(Report.share_token_hash == _hash(token)))
        ).scalar_one_or_none()
        if (
            report is None
            or report.html_artifact_id is None
            or report.share_expires_at is None
            or report.share_expires_at < datetime.now(UTC)
        ):
            raise AppError("NOT_FOUND", "link not found or expired")
        art = await artifacts.get(session, report.html_artifact_id)
        return artifacts.read(art)
