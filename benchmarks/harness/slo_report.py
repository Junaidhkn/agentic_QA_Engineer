"""Beta-exit SLO report (plan Phase 11: "one month of beta meeting NFR-3/NFR-4").

Queries the platform tenant in Mimir (Prometheus HTTP API) over the last ``--days`` and checks
each launch criterion. The SLIs are the ones the alerts use (deploy/observability/rules).

    uv run python benchmarks/harness/slo_report.py --prometheus-url https://mimir.internal/prometheus

Exit code 0 only when every criterion passes. A criterion with no data FAILS — a month without
traffic or scrapes proves nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

READS = 'method="GET",route!~".*/events|/internal/.*"'


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    query: str  # {w} is the window, e.g. 30d
    passes: Callable[[float], bool]
    target: str
    fmt: Callable[[float], str]


def pct(v: float) -> str:
    return f"{v * 100:.3f} %"


CRITERIA = [
    Criterion(
        "availability",
        "Control-plane availability (NFR-3)",
        '1 - sum(increase(re_http_requests_total{{status="5xx"}}[{w}])) / sum(increase(re_http_requests_total[{w}]))',
        lambda v: v >= 0.999,
        "≥ 99.9 %",
        pct,
    ),
    Criterion(
        "read_latency",
        "API reads under 300 ms (NFR-5)",
        f'sum(increase(re_http_request_duration_seconds_bucket{{{{{READS},le="0.3"}}}}[{{w}}]))'
        f" / sum(increase(re_http_request_duration_seconds_count{{{{{READS}}}}}[{{w}}]))",
        lambda v: v >= 0.95,
        "≥ 95 %",
        pct,
    ),
    Criterion(
        "job_start",
        "Jobs started within 30 s (NFR-4)",
        'sum(increase(re_job_start_seconds_bucket{{le="30.0"}}[{w}])) / sum(increase(re_job_start_seconds_count[{w}]))',
        lambda v: v >= 0.95,
        "≥ 95 %",
        pct,
    ),
    Criterion(
        "kill_latency",
        "Kills slower than 10 s (NFR-7)",
        "sum(increase(re_kill_latency_seconds_count[{w}]))"
        ' - sum(increase(re_kill_latency_seconds_bucket{{le="10.0"}}[{w}]))',
        lambda v: round(v) == 0,
        "0",
        lambda v: f"{v:.0f}",
    ),
    Criterion(
        "canary",
        "Synthetic canary passing (share of 15-min samples)",
        'avg_over_time((max(re_platform_check_ok{{check="canary"}}))[{w}:15m])',
        lambda v: v >= 0.99,
        "≥ 99 %",
        pct,
    ),
    Criterion(
        "integrity",
        "Worst integrity-violation count in the window",
        "max_over_time((sum(max by (invariant) (re_integrity_violations)))[{w}:1h])",
        lambda v: round(v) == 0,
        "0",
        lambda v: f"{v:.0f}",
    ),
    Criterion(
        "restore_drill",
        "Days since the last successful restore drill",
        '(time() - max(re_platform_check_last_success_timestamp_seconds{{check="restore_drill"}})) / 86400',
        lambda v: v <= 8,
        "≤ 8",
        lambda v: f"{v:.1f}",
    ),
    Criterion(
        "rls_denials",
        "Row-level-security denials",
        "sum(increase(re_rls_denials_total[{w}]))",
        lambda v: round(v) == 0,
        "0",
        lambda v: f"{v:.0f}",
    ),
]


def query(client: httpx.Client, promql: str) -> float | None:
    r = client.get("/api/v1/query", params={"query": promql})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"query failed: {body.get('error')}")
    result = body["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else value  # NaN (0/0) = no data


def report(client: httpx.Client, days: int) -> dict[str, Any]:
    rows = []
    for c in CRITERIA:
        promql = c.query.format(w=f"{days}d")
        value = query(client, promql)
        rows.append(
            {
                "key": c.key,
                "title": c.title,
                "target": c.target,
                "value": value,
                "display": "no data" if value is None else c.fmt(value),
                "passed": value is not None and c.passes(value),
                "query": promql,
            }
        )
    return {"days": days, "criteria": rows, "passed": all(r["passed"] for r in rows)}


def markdown(rep: dict[str, Any]) -> str:
    lines = [
        f"# SLO report — last {rep['days']} days",
        "",
        "| Criterion | Target | Measured | |",
        "|---|---|---|---|",
        *[
            f"| {r['title']} | {r['target']} | {r['display']} | {'✅' if r['passed'] else '❌'} |"
            for r in rep["criteria"]
        ],
        "",
        f"**{'PASS' if rep['passed'] else 'FAIL'}** — beta exit requires every row to pass.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--prometheus-url", required=True, help="Mimir Prometheus API base, e.g. https://mimir/prometheus")
    p.add_argument("--tenant", default="platform", help="X-Scope-OrgID")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--token", help="bearer token for the query endpoint, if any")
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "out")
    args = p.parse_args(argv)
    headers = {"X-Scope-OrgID": args.tenant} | ({"Authorization": f"Bearer {args.token}"} if args.token else {})
    with httpx.Client(base_url=args.prometheus_url, headers=headers, timeout=60) as client:
        rep = report(client, args.days)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "slo-report.json").write_text(json.dumps(rep, indent=2) + "\n")
    (args.out / "slo-report.md").write_text(markdown(rep))
    print(markdown(rep))
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
