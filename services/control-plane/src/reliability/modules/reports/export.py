"""Report export (FR-28, UF-7): one deterministic JSON document per report (schema
``report@1``) and a self-contained HTML rendering of it (inline CSS and SVG, no scripts, every
value escaped) that is safe to share via a link.

System runs — diagnosis experiments, verification and A/A noise-floor runs — are not the
customer's test history and are left out of run history and trends.
"""

from __future__ import annotations

import html
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.discovery import service as discovery
from reliability.modules.findings.models import Finding
from reliability.modules.findings.service import ACTIVE
from reliability.modules.integrations.github.models import PullRequest
from reliability.modules.projects.models import Project
from reliability.modules.repairs.models import RepairAttempt
from reliability.modules.runs.models import Run, RunMetricSummary
from reliability.modules.verification.models import PerformanceComparison, VerificationGate

SCHEMA = "report@1"
SECTIONS = ("runs", "trends", "coverage", "findings", "repairs")
SYSTEM_RUN_KEYS = ("experiment", "verification", "noise_floor")
MAX_RUNS = 200
MAX_TREND_SCOPES = 8


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _num(v: Any) -> float | None:
    return None if v is None else round(float(v), 4)


def is_system_run(run: Run) -> bool:
    cfg = run.config_snapshot or {}
    return any(cfg.get(k) for k in SYSTEM_RUN_KEYS)


async def build(
    session: AsyncSession,
    project_id: uuid.UUID,
    start: datetime,
    end: datetime,
    sections: list[str] | None,
    now: datetime,
) -> dict[str, Any]:
    wanted = [s for s in SECTIONS if s in (sections or SECTIONS)]
    project = await session.get(Project, project_id)
    assert project is not None
    runs = [
        r
        for r in (
            await session.execute(
                select(Run)
                .where(Run.project_id == project_id, Run.created_at >= start, Run.created_at < end)
                .order_by(Run.created_at.desc())
            )
        ).scalars()
        if not is_system_run(r)
    ]
    steady = await _steady(session, [r.id for r in runs])
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "project": {"id": str(project.id), "name": project.name},
        "period": {"from": _iso(start), "to": _iso(end)},
        "generated_at": _iso(now),
        "sections": wanted,
        "summary": await _summary(session, project_id, runs),
    }
    if "runs" in wanted:
        doc["runs"] = [_run_row(r, steady.get(r.id, {})) for r in runs[:MAX_RUNS]]
    if "trends" in wanted:
        doc["trends"] = _trends(runs, steady)
    if "coverage" in wanted:
        doc["coverage"] = await discovery.coverage(session, project_id)
    if "findings" in wanted:
        doc["findings"] = await _findings(session, project_id, start, end)
    if "repairs" in wanted:
        doc["repairs"] = await _repairs(session, project_id, start, end)
    return doc


async def _steady(session: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, RunMetricSummary]]:
    out: dict[uuid.UUID, dict[str, RunMetricSummary]] = {}
    if not run_ids:
        return out
    rows = (
        await session.execute(
            select(RunMetricSummary).where(
                RunMetricSummary.run_id.in_(run_ids), RunMetricSummary.time_window == "steady_state"
            )
        )
    ).scalars()
    for m in rows:
        out.setdefault(m.run_id, {})[m.scope] = m
    return out


async def _summary(session: AsyncSession, project_id: uuid.UUID, runs: list[Run]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    for r in runs:
        k = by_kind.setdefault(r.kind, {})
        k[r.status] = k.get(r.status, 0) + 1
    active = (
        await session.execute(
            select(Finding.severity).where(Finding.project_id == project_id, Finding.status.in_(ACTIVE))
        )
    ).scalars()
    severities: dict[str, int] = {}
    for s in active:
        severities[s] = severities.get(s, 0) + 1
    capacity = next(((r.summary or {}).get("knee") for r in runs if (r.summary or {}).get("knee")), None)
    return {
        "runs": {kind: dict(sorted(v.items())) for kind, v in sorted(by_kind.items())},
        "open_findings": dict(sorted(severities.items())),
        "capacity": capacity,
    }


def _run_row(r: Run, scopes: dict[str, RunMetricSummary]) -> dict[str, Any]:
    overall = scopes.get("overall")
    return {
        "id": str(r.id),
        "kind": r.kind,
        "status": r.status,
        "environment": r.environment_kind,
        "created_at": _iso(r.created_at),
        "finished_at": _iso(r.finished_at),
        "p95_ms": _num(overall.p95_ms) if overall else None,
        "error_rate": _num(overall.error_rate) if overall else None,
        "rps": _num(overall.rps) if overall else None,
    }


def _trends(runs: list[Run], steady: dict[uuid.UUID, dict[str, RunMetricSummary]]) -> dict[str, list[dict[str, Any]]]:
    series: dict[str, list[dict[str, Any]]] = {}
    for r in sorted((r for r in runs if r.kind == "load"), key=lambda r: r.created_at):
        for scope, m in steady.get(r.id, {}).items():
            if scope == "overall" or scope.startswith(("endpoint:", "scenario:")):
                series.setdefault(scope, []).append(
                    {
                        "run_id": str(r.id),
                        "at": _iso(r.created_at),
                        "p95_ms": _num(m.p95_ms),
                        "error_rate": _num(m.error_rate),
                    }
                )
    ranked = sorted(
        (s for s in series if s != "overall"),
        key=lambda s: (-(series[s][-1]["p95_ms"] or 0), s),
    )[:MAX_TREND_SCOPES]
    return {s: series[s] for s in (["overall"] if "overall" in series else []) + ranked}


async def _findings(
    session: AsyncSession, project_id: uuid.UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Finding)
            .where(
                Finding.project_id == project_id,
                or_(Finding.status.in_(ACTIVE), Finding.updated_at >= start),
                Finding.created_at < end,
            )
            .order_by(Finding.created_at)
        )
    ).scalars()
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    out = [
        {
            "id": str(f.id),
            "title": f.title,
            "type": f.type,
            "severity": f.severity,
            "status": f.status,
            "occurrences": f.occurrences,
            "first_seen": _iso(f.created_at),
            "last_seen": _iso(f.updated_at),
        }
        for f in rows
    ]
    return sorted(out, key=lambda f: (order.get(f["severity"], 9), f["first_seen"] or "", f["id"]))


async def _repairs(
    session: AsyncSession, project_id: uuid.UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    attempts = list(
        (
            await session.execute(
                select(RepairAttempt)
                .where(
                    RepairAttempt.project_id == project_id,
                    RepairAttempt.created_at >= start,
                    RepairAttempt.created_at < end,
                )
                .order_by(RepairAttempt.created_at)
            )
        ).scalars()
    )
    out = []
    for a in attempts:
        gates = (
            await session.execute(select(VerificationGate).where(VerificationGate.repair_attempt_id == a.id))
        ).scalars()
        comps = (
            await session.execute(select(PerformanceComparison).where(PerformanceComparison.repair_attempt_id == a.id))
        ).scalars()
        pr = (
            await session.execute(select(PullRequest).where(PullRequest.repair_attempt_id == a.id))
        ).scalar_one_or_none()
        out.append(
            {
                "id": str(a.id),
                "finding_id": str(a.finding_id),
                "attempt_no": a.attempt_no,
                "status": a.status,
                "diff_stats": a.diff_stats,
                "gates": {g.gate: g.status for g in sorted(gates, key=lambda g: g.gate) if g.applicable},
                "comparisons": [
                    {
                        "scope": c.scope,
                        "baseline_p95_ms": _num(c.baseline_value),
                        "candidate_p95_ms": _num(c.candidate_value),
                        "delta_pct": _num(c.delta_pct),
                        "ci_pct": [_num(c.ci_low_pct), _num(c.ci_high_pct)],
                        "verdict": c.verdict,
                    }
                    for c in sorted(comps, key=lambda c: c.scope)
                ],
                "pull_request": pr.url if pr and pr.state == "open" else None,
            }
        )
    return out


# ----------------------------------------------------------------- HTML ----
CSS = """
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--border:#d1d9e0;--surface:#f6f8fa;--accent:#0969da;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--border:#3d444d;
--surface:#151b23;--accent:#4493f8;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid var(--border);padding-bottom:4px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.tile{border:1px solid var(--border);background:var(--surface);border-radius:8px;padding:12px}
.tile b{display:block;font-size:20px}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:600}.scroll{overflow-x:auto}code{font-size:12px}
.s-passed,.s-improved,.s-verified,.s-resolved,.s-pr_opened{color:var(--ok)}
.s-failed,.s-errored,.s-regressed,.s-critical,.s-high,.s-verification_failed{color:var(--bad)}
.s-medium,.s-inconclusive,.s-awaiting_approval,.s-open{color:var(--warn)}
svg.spark{width:160px;height:36px}svg.spark polyline{fill:none;stroke:var(--accent);stroke-width:2}
"""


def _e(v: Any) -> str:
    return html.escape("—" if v is None else str(v), quote=True)


def _status(v: Any) -> str:
    cls = "".join(ch for ch in str(v) if ch.isalnum() or ch == "_")
    return f'<span class="s-{cls}">{_e(str(v).replace("_", " "))}</span>'


def _fmt(v: Any, unit: str = "", digits: int = 1) -> str:
    if v is None:
        return "—"
    return _e(f"{float(v):.{digits}f}{unit}")


def _spark(points: list[float | None]) -> str:
    vals = [p for p in points if p is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = 156 / (len(vals) - 1)
    coords = " ".join(f"{2 + i * step:.1f},{34 - (v - lo) / span * 32:.1f}" for i, v in enumerate(vals))
    return f'<svg class="spark" viewBox="0 0 160 36" role="img" aria-label="trend"><polyline points="{coords}"/></svg>'


def _table(head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="muted">Nothing in this period.</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def render_html(doc: dict[str, Any]) -> str:
    s = doc["summary"]
    total_runs = sum(n for k in s["runs"].values() for n in k.values())
    passed = sum(k.get("passed", 0) for k in s["runs"].values())
    open_findings = sum(s["open_findings"].values())
    parts = [
        f"<h1>Reliability report — {_e(doc['project']['name'])}</h1>",
        f'<p class="muted">{_e(doc["period"]["from"])} → {_e(doc["period"]["to"])} · generated '
        f"{_e(doc['generated_at'])} · schema {_e(doc['schema'])}</p>",
        '<div class="grid">',
        f'<div class="tile"><span class="muted">Runs</span><b>{total_runs}</b>{passed} passed</div>',
        f'<div class="tile"><span class="muted">Open findings</span><b>{open_findings}</b>'
        + ", ".join(f"{_e(n)} {_e(k)}" for k, n in s["open_findings"].items())
        + "</div>",
    ]
    if s.get("capacity"):
        cap = s["capacity"]
        parts.append(
            f'<div class="tile"><span class="muted">Capacity estimate</span><b>{_e(cap.get("vus"))} VUs</b>'
            f"{_e(cap.get('reason'))}</div>"
        )
    if "coverage" in doc:
        t = doc["coverage"]["totals"]
        parts.append(
            f'<div class="tile"><span class="muted">Workflows</span><b>{_e(t["passing"])} passing</b>'
            f"{_e(t['discovered'])} discovered · {_e(t['approved'])} approved · {_e(t['tested'])} tested</div>"
        )
    parts.append("</div>")
    if "trends" in doc:
        rows = []
        for scope, pts in doc["trends"].items():
            last = pts[-1] if pts else {}
            rows.append(
                [
                    f"<code>{_e(scope)}</code>",
                    _spark([p["p95_ms"] for p in pts]),
                    _fmt(last.get("p95_ms"), " ms"),
                    _fmt((last.get("error_rate") or 0) * 100, "%", 2),
                    _e(len(pts)),
                ]
            )
        parts += [
            "<h2>Trends (steady-state p95 per load run)</h2>",
            _table(["Scope", "p95", "Latest p95", "Errors", "Runs"], rows),
        ]
    if "findings" in doc:
        parts += [
            "<h2>Findings</h2>",
            _table(
                ["Severity", "Title", "Type", "Status", "Seen", "First seen"],
                [
                    [
                        _status(f["severity"]),
                        _e(f["title"]),
                        _e(f["type"].replace("_", " ")),
                        _status(f["status"]),
                        _e(f["occurrences"]),
                        _e(f["first_seen"]),
                    ]
                    for f in doc["findings"]
                ],
            ),
        ]
    if "repairs" in doc:
        rows = []
        for r in doc["repairs"]:
            comp = next((c for c in r["comparisons"] if c["scope"].startswith("endpoint:")), None)
            change = (
                f"{_fmt(comp['baseline_p95_ms'], ' ms')} → {_fmt(comp['candidate_p95_ms'], ' ms')} "
                f"({_fmt(comp['delta_pct'], '%')}, {_status(comp['verdict'])})"
                if comp
                else "—"
            )
            gates = ", ".join(f"{_e(g.replace('_', ' '))}: {_status(v)}" for g, v in r["gates"].items()) or "—"
            pr = f'<a href="{_e(r["pull_request"])}">pull request</a>' if r["pull_request"] else "—"
            rows.append([_e(f"#{r['attempt_no']}"), _status(r["status"]), gates, change, pr])
        parts += ["<h2>Repairs</h2>", _table(["Attempt", "Status", "Gates", "Before → after (p95)", "PR"], rows)]
    if "coverage" in doc:
        cov = doc["coverage"]
        rows = [
            [
                _e(src),
                _e(c.get("discovered", 0)),
                _e(c.get("approved", 0)),
                _e(c.get("tested", 0)),
                _e(c.get("passing", 0)),
            ]
            for src, c in sorted(cov.get("by_source", {}).items())
        ]
        parts += [
            "<h2>Coverage (workflows, counts — not a percentage of the application)</h2>",
            _table(["Source", "Discovered", "Approved", "Tested", "Passing"], rows),
        ]
    if "runs" in doc:
        parts += [
            "<h2>Run history</h2>",
            _table(
                ["Started", "Kind", "Environment", "Status", "p95", "Errors", "Req/s"],
                [
                    [
                        _e(r["created_at"]),
                        _e(r["kind"]),
                        _e(r["environment"].replace("_", " ")),
                        _status(r["status"]),
                        _fmt(r["p95_ms"], " ms"),
                        _fmt((r["error_rate"] or 0) * 100 if r["error_rate"] is not None else None, "%", 2),
                        _fmt(r["rps"]),
                    ]
                    for r in doc["runs"]
                ],
            ),
        ]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        f"<title>Reliability report — {_e(doc['project']['name'])}</title><style>{CSS}</style></head>"
        f"<body><main>{''.join(parts)}</main></body></html>"
    )
