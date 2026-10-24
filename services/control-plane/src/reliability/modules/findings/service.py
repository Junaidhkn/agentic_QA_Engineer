"""Findings: deterministic evaluation of finished runs, de-duplication, evidence and resolution (FR-20)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.findings.detectors import (
    Detected,
    EvidenceSpec,
    ScopeRow,
    detect_functional,
    detect_load,
    detect_probes,
)
from reliability.modules.findings.models import Evidence, Finding
from reliability.modules.runs.models import Run, RunMetricSummary
from reliability.modules.runs.timeseries import TimeSeries, Window
from reliability.modules.workflows.models import Workflow
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
ACTIVE = ("open", "diagnosing", "diagnosed", "fixing", "fix_proposed")
BASELINE_RUNS = 5


async def _rows(session: AsyncSession, run_id: uuid.UUID, window: str) -> dict[str, ScopeRow]:
    rows = (
        await session.execute(
            select(RunMetricSummary).where(RunMetricSummary.run_id == run_id, RunMetricSummary.time_window == window)
        )
    ).scalars()
    return {
        r.scope: ScopeRow(
            r.scope,
            r.requests,
            float(r.rps),
            float(r.error_rate),
            float(r.p50_ms) if r.p50_ms is not None else None,
            float(r.p95_ms) if r.p95_ms is not None else None,
            float(r.p99_ms) if r.p99_ms is not None else None,
        )
        for r in rows
    }


async def _baseline(session: AsyncSession, run: Run) -> dict[str, list[float]]:
    profile_id = run.load_profile_id
    if profile_id is None:
        return {}
    prior = list(
        (
            await session.execute(
                select(Run.id)
                .where(
                    Run.load_profile_id == profile_id,
                    Run.id != run.id,
                    Run.status == "passed",
                    Run.kind == "load",
                    Run.target_id == run.target_id,
                    Run.created_at < run.created_at,
                )
                .order_by(Run.created_at.desc())
                .limit(BASELINE_RUNS)
            )
        ).scalars()
    )
    out: dict[str, list[float]] = {}
    for rid in prior:
        for scope, row in (await _rows(session, rid, "steady_state")).items():
            if row.p95_ms is not None:
                out.setdefault(scope, []).append(row.p95_ms)
    return out


def _load_artifact_json(arts: list[artifacts.Artifact], kind: str, name: str) -> dict[str, Any] | None:
    for a in arts:
        if a.kind == kind and a.storage_key.endswith("/" + name):
            try:
                data: dict[str, Any] = json.loads(artifacts.read(a))
                return data
            except (OSError, ValueError):
                return None
    return None


async def evaluate_run(session: AsyncSession, run: Run) -> list[Finding]:
    if run.status not in ("passed", "failed"):
        return []  # invalid / errored / cancelled runs do not produce findings
    if (run.config_snapshot or {}).get("experiment"):
        return []  # diagnosis experiments measure a hypothesis; they are not new findings
    arts = await artifacts.for_run(session, run.id)
    summary = run.summary or {}
    wfs = {
        w.key: w
        for w in (await session.execute(select(Workflow).where(Workflow.project_id == run.project_id))).scalars()
    }
    detected: list[Detected] = []
    covered: set[str] = set()
    if run.kind == "load":
        ts_data = _load_artifact_json(arts, "k6_timeseries", "timeseries.json")
        ts = TimeSeries(ts_data) if ts_data else None
        window_s = (summary.get("steady_state") or {}).get("window_s")
        window = Window(*window_s) if window_s else None
        rows = await _rows(session, run.id, "steady_state") or await _rows(session, run.id, "full")
        thresholds = (run.config_snapshot.get("profile") or {}).get("thresholds") or {}
        detected = detect_load(str(run.id), rows, thresholds, ts, window, await _baseline(session, run))
        covered = {f"slo:{s}:" for s in rows} | {f"regression:{s}:" for s in rows} | {"spike:overall"}
    elif run.kind == "functional":
        k6_summary = _load_artifact_json(arts, "k6_summary", "summary.json")
        trace = next((a for a in arts if a.kind == "playwright_trace"), None)
        detected = detect_functional(
            str(run.id),
            summary,
            k6_summary,
            {k: w.criticality for k, w in wfs.items()},
            str(trace.id) if trace else None,
        )
        covered = (
            {f"func:{t.get('file')}:{t.get('title')}" for t in summary.get("tests") or []}
            | {f"flaky:{t.get('file')}:{t.get('title')}" for t in summary.get("tests") or []}
            | {"func:api:"}
        )
    elif run.kind == "probe":
        detected = detect_probes(str(run.id), summary)
        covered = {f"invariant:{name}" for name in summary.get("probes") or {}}

    findings = [await _upsert(session, run, d, wfs) for d in detected]
    await _resolve_missing(session, run, {d.fingerprint for d in detected}, covered)
    return findings


async def _upsert(session: AsyncSession, run: Run, d: Detected, wfs: dict[str, Workflow]) -> Finding:
    existing = (
        await session.execute(
            select(Finding)
            .where(Finding.project_id == run.project_id, Finding.fingerprint == d.fingerprint)
            .with_for_update()
        )
    ).scalar_one_or_none()
    wf = wfs.get(d.workflow_key or "")
    if existing is None:
        finding = Finding(
            id=uuid7(),
            org_id=run.org_id,
            project_id=run.project_id,
            type=d.type,
            severity=d.severity,
            fingerprint=d.fingerprint,
            title=d.title,
            status="open",
            workflow_id=wf.id if wf else None,
            first_run_id=run.id,
            last_run_id=run.id,
            occurrences=1,
            headline=d.headline,
        )
        session.add(finding)
    else:
        finding = existing
        finding.occurrences += 1
        finding.last_run_id, finding.headline, finding.title = run.id, d.headline, d.title
        if SEVERITY_ORDER[d.severity] > SEVERITY_ORDER[finding.severity]:
            finding.severity = d.severity
        if finding.status in ("resolved", "duplicate"):
            finding.status, finding.resolved_by_run_id = "open", None
    await session.flush()
    for spec in d.evidence:
        await add_evidence(session, finding, run.id, spec, "detector")
    return finding


async def add_evidence(
    session: AsyncSession,
    finding: Finding | None,
    run_id: uuid.UUID | None,
    spec: EvidenceSpec,
    collected_by: str,
    org_id: uuid.UUID | None = None,
) -> Evidence:
    ev = Evidence(
        id=uuid7(),
        org_id=finding.org_id if finding else org_id,
        finding_id=finding.id if finding else None,
        run_id=run_id,
        kind=spec.kind,
        locator=spec.locator,
        summary=spec.summary[:2000],
        snapshot=json.loads(json.dumps(spec.snapshot, default=str)) if spec.snapshot is not None else None,
        collected_by=collected_by,
    )
    session.add(ev)
    await session.flush()
    return ev


async def _resolve_missing(session: AsyncSession, run: Run, detected: set[str], covered: set[str]) -> None:
    active = (
        await session.execute(
            select(Finding).where(Finding.project_id == run.project_id, Finding.status.in_(("open", "diagnosed")))
        )
    ).scalars()
    for f in active:
        if f.fingerprint in detected:
            continue
        if any(f.fingerprint == c or (c.endswith(":") and f.fingerprint.startswith(c)) for c in covered):
            f.status, f.resolved_by_run_id = "resolved", run.id
            f.updated_at = datetime.now(UTC)


async def get(session: AsyncSession, finding_id: uuid.UUID) -> Finding:
    f = await session.get(Finding, finding_id)
    if f is None:
        raise AppError("NOT_FOUND", "finding not found")
    return f


async def list_for(
    session: AsyncSession, project_id: uuid.UUID, status: str | None = None, type_: str | None = None
) -> list[Finding]:
    stmt = select(Finding).where(Finding.project_id == project_id)
    if status == "active":
        stmt = stmt.where(Finding.status.in_(ACTIVE))
    elif status:
        stmt = stmt.where(Finding.status == status)
    if type_:
        stmt = stmt.where(Finding.type == type_)
    rows = list((await session.execute(stmt.order_by(Finding.updated_at.desc()))).scalars())
    return sorted(rows, key=lambda f: (f.status not in ACTIVE, -SEVERITY_ORDER[f.severity]))


async def evidence_for(session: AsyncSession, finding_id: uuid.UUID) -> list[Evidence]:
    return list(
        (
            await session.execute(
                select(Evidence).where(Evidence.finding_id == finding_id).order_by(Evidence.created_at)
            )
        ).scalars()
    )
