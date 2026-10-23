"""Deterministic finding detectors (FR-20, low-level-design.md §8).

Pure functions: run outputs in, findings with evidence specs out. No LLM involved, so the
same run always yields the same findings.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from reliability.modules.runs.timeseries import TimeSeries, Window

REGRESSION_MIN_PCT = 0.10
REGRESSION_MIN_ABS_MS = 20.0
SPIKE_MIN_RATE = 0.05
SPIKE_FACTOR = 3.0
SPIKE_MIN_REQUESTS = 20
SERIES_POINTS = 240


@dataclass
class EvidenceSpec:
    kind: str
    locator: dict[str, Any]
    summary: str
    snapshot: dict[str, Any] | None = None


@dataclass
class Detected:
    type: str
    severity: str
    fingerprint: str
    title: str
    headline: dict[str, Any]
    workflow_key: str | None = None
    evidence: list[EvidenceSpec] = field(default_factory=list)


@dataclass(frozen=True)
class ScopeRow:
    scope: str
    requests: int
    rps: float
    error_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


def _fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f} ms"


def _label(scope: str) -> str:
    return scope.split(":", 1)[1] if ":" in scope else "all requests"


def _max_vus(ts: TimeSeries | None, window: Window | None) -> int | None:
    if ts is None or not ts.vus:
        return None
    if window is None:
        return max(ts.vus.values())
    lo, hi = int(window.start_s // ts.bucket_s), int(window.end_s // ts.bucket_s)
    values = [v for b, v in ts.vus.items() if lo <= b <= hi]
    return max(values) if values else None


def _metric_evidence(
    run_id: str, scope: str, row: ScopeRow, window: Window | None, ts: TimeSeries | None
) -> EvidenceSpec:
    series = ts.series(scope)[:SERIES_POINTS] if ts else []
    return EvidenceSpec(
        kind="metric_window",
        locator={
            "run_id": run_id,
            "scope": scope,
            "window_s": [window.start_s, window.end_s] if window else None,
            "source": "run_timeseries",
        },
        summary=(
            f"{_label(scope)}: p50 {_fmt_ms(row.p50_ms)}, p95 {_fmt_ms(row.p95_ms)}, p99 {_fmt_ms(row.p99_ms)}, "
            f"errors {row.error_rate:.2%}, {row.rps:.1f} req/s over {row.requests} requests (steady state)"
        ),
        snapshot={
            "p50_ms": row.p50_ms,
            "p95_ms": row.p95_ms,
            "p99_ms": row.p99_ms,
            "error_rate": row.error_rate,
            "rps": row.rps,
            "requests": row.requests,
            "series": series,
        },
    )


def _sample_evidence(
    run_id: str, ts: TimeSeries | None, endpoint: str | None, kind: str, limit: int = 5
) -> list[EvidenceSpec]:
    if ts is None:
        return []
    pool = ts.samples.get("errors" if kind == "errors" else "slowest", [])
    picked = [s for s in pool if endpoint is None or s.get("endpoint") == endpoint][:limit]
    out = []
    for s in picked:
        tid = s.get("trace_id")
        out.append(
            EvidenceSpec(
                kind="http_sample",
                locator={"run_id": run_id, "trace_id": tid, "endpoint": s.get("endpoint"), "t_s": s.get("t")},
                summary=(
                    f"{s.get('endpoint')} took {s.get('duration_ms')} ms, status {s.get('status') or 'n/a'}"
                    f"{', error ' + s['error'] if s.get('error') else ''} at t+{s.get('t')}s"
                    f"{' (trace ' + tid[:16] + '…)' if tid else ''}"
                ),
                snapshot=s,
            )
        )
    return out


def detect_load(
    run_id: str,
    rows: dict[str, ScopeRow],
    thresholds: dict[str, float],
    ts: TimeSeries | None,
    window: Window | None,
    baseline: dict[str, list[float]],
) -> list[Detected]:
    p95_limit = float(thresholds.get("p95_ms", 500))
    err_limit = float(thresholds.get("error_rate", 0.01))
    vus = _max_vus(ts, window)
    out: list[Detected] = []
    endpoint_breaches = False
    for scope, row in sorted(rows.items()):
        if not scope.startswith("endpoint:") or row.requests == 0:
            continue
        endpoint = _label(scope)
        if row.p95_ms is not None and row.p95_ms > p95_limit:
            endpoint_breaches = True
            ratio = row.p95_ms / p95_limit
            out.append(
                Detected(
                    "slo_breach",
                    "high" if ratio >= 2 else "medium",
                    f"slo:{scope}:p95",
                    f"{endpoint} p95 {row.p95_ms:.0f} ms exceeds {p95_limit:.0f} ms"
                    + (f" at {vus} VUs" if vus else ""),
                    {"metric": "p95_ms", "observed": row.p95_ms, "threshold": p95_limit, "at_vus": vus, "scope": scope},
                    evidence=[
                        _metric_evidence(run_id, scope, row, window, ts),
                        *_sample_evidence(run_id, ts, endpoint, "slowest"),
                    ],
                )
            )
        if row.error_rate > err_limit:
            endpoint_breaches = True
            out.append(
                Detected(
                    "slo_breach",
                    "high" if row.error_rate >= 0.05 else "medium",
                    f"slo:{scope}:errors",
                    f"{endpoint} fails {row.error_rate:.1%} of requests (limit {err_limit:.1%})"
                    + (f" at {vus} VUs" if vus else ""),
                    {
                        "metric": "error_rate",
                        "observed": row.error_rate,
                        "threshold": err_limit,
                        "at_vus": vus,
                        "scope": scope,
                    },
                    evidence=[
                        _metric_evidence(run_id, scope, row, window, ts),
                        *_sample_evidence(run_id, ts, endpoint, "errors"),
                    ],
                )
            )
        prior = baseline.get(scope) or []
        if row.p95_ms is not None and len(prior) >= 3:
            median = statistics.median(prior)
            noise = statistics.pstdev(prior) / median if median else 0
            limit = median * (1 + max(REGRESSION_MIN_PCT, 2 * noise))
            if row.p95_ms > limit and row.p95_ms - median > REGRESSION_MIN_ABS_MS:
                delta = (row.p95_ms - median) / median
                out.append(
                    Detected(
                        "regression",
                        "high" if delta >= 0.5 else "medium",
                        f"regression:{scope}:p95",
                        f"{endpoint} p95 regressed {delta:.0%} vs. baseline ({median:.0f} → {row.p95_ms:.0f} ms)",
                        {
                            "metric": "p95_ms",
                            "observed": row.p95_ms,
                            "baseline_median": median,
                            "baseline_runs": len(prior),
                            "noise_pct": round(noise * 100, 1),
                            "delta_pct": round(delta * 100, 1),
                            "scope": scope,
                        },
                        evidence=[
                            _metric_evidence(run_id, scope, row, window, ts),
                            EvidenceSpec(
                                "metric_window",
                                {"scope": scope, "source": "baseline", "runs": len(prior)},
                                f"baseline p95 over last {len(prior)} comparable runs: median {median:.0f} ms"
                                f" (spread ±{noise:.0%})",
                                {"values": prior},
                            ),
                        ],
                    )
                )
    overall = rows.get("overall")
    if overall and not endpoint_breaches and overall.requests:
        if overall.p95_ms is not None and overall.p95_ms > p95_limit:
            out.append(
                Detected(
                    "slo_breach",
                    "medium",
                    "slo:overall:p95",
                    f"Overall p95 {overall.p95_ms:.0f} ms exceeds {p95_limit:.0f} ms",
                    {
                        "metric": "p95_ms",
                        "observed": overall.p95_ms,
                        "threshold": p95_limit,
                        "at_vus": vus,
                        "scope": "overall",
                    },
                    evidence=[
                        _metric_evidence(run_id, "overall", overall, window, ts),
                        *_sample_evidence(run_id, ts, None, "slowest"),
                    ],
                )
            )
        if overall.error_rate > err_limit:
            out.append(
                Detected(
                    "slo_breach",
                    "high",
                    "slo:overall:errors",
                    f"{overall.error_rate:.1%} of requests failed (limit {err_limit:.1%})",
                    {
                        "metric": "error_rate",
                        "observed": overall.error_rate,
                        "threshold": err_limit,
                        "at_vus": vus,
                        "scope": "overall",
                    },
                    evidence=[
                        _metric_evidence(run_id, "overall", overall, window, ts),
                        *_sample_evidence(run_id, ts, None, "errors"),
                    ],
                )
            )
    if ts is not None:
        spike = _error_spike(ts, window)
        if spike:
            out.append(
                Detected(
                    "error_spike",
                    "high",
                    "spike:overall",
                    f"Error spike: {spike['peak_rate']:.0%} of requests failed around t+{spike['start_s']}s",
                    spike,
                    evidence=[
                        EvidenceSpec(
                            "metric_window",
                            {
                                "run_id": run_id,
                                "scope": "overall",
                                "window_s": [spike["start_s"], spike["end_s"]],
                                "source": "run_timeseries",
                            },
                            f"error rate peaked at {spike['peak_rate']:.0%} between "
                            f"t+{spike['start_s']}s and t+{spike['end_s']}s "
                            f"(baseline {spike['median_rate']:.1%})",
                            spike,
                        ),
                        *_sample_evidence(run_id, ts, None, "errors"),
                    ],
                )
            )
    return out


def _error_spike(ts: TimeSeries, window: Window | None) -> dict[str, Any] | None:
    points = [p for p in ts.series("overall") if p["rps"] * ts.bucket_s >= SPIKE_MIN_REQUESTS]
    if window:
        points = [p for p in points if window.start_s <= p["t"] <= window.end_s]
    if len(points) < 6:
        return None
    median = statistics.median(p["error_rate"] for p in points)
    limit = max(SPIKE_MIN_RATE, SPIKE_FACTOR * median)
    run_start, best = None, None
    for i, p in enumerate(points):
        if p["error_rate"] > limit:
            run_start = i if run_start is None else run_start
            if i - run_start + 1 >= 2:
                seg = points[run_start : i + 1]
                best = {
                    "start_s": seg[0]["t"],
                    "end_s": seg[-1]["t"] + ts.bucket_s,
                    "peak_rate": max(q["error_rate"] for q in seg),
                    "median_rate": median,
                }
        else:
            run_start = None
    if best and median > 0.5 * best["peak_rate"]:
        return None  # sustained failure, not a spike — the SLO breach covers it
    return best


def _checks(group: dict[str, Any]) -> list[tuple[str, int, int]]:
    out = []
    checks = group.get("checks") or {}
    items = checks.values() if isinstance(checks, dict) else checks
    for c in items:
        out.append((c.get("name", ""), int(c.get("passes", 0)), int(c.get("fails", 0))))
    groups = group.get("groups") or {}
    for g in groups.values() if isinstance(groups, dict) else groups:
        out.extend(_checks(g))
    return out


def detect_functional(
    run_id: str,
    summary: dict[str, Any],
    k6_summary: dict[str, Any] | None,
    criticality: dict[str, str],
    trace_artifact_id: str | None,
) -> list[Detected]:
    out: list[Detected] = []
    sev = {"critical": "critical", "high": "high", "normal": "medium"}
    for test in summary.get("tests") or []:
        status = test.get("status")
        if status not in ("unexpected", "flaky"):
            continue
        file_key = str(test.get("file", "")).rsplit("/", 1)[-1].replace(".spec.ts", "")
        wf_key = next((k for k in criticality if file_key == "wf_" + k.replace(".", "_").replace("-", "_")), None)
        evidence = [
            EvidenceSpec(
                "assertion",
                {"run_id": run_id, "test": test.get("title"), "file": test.get("file")},
                (test.get("error") or "test failed")[:500],
                {"error": test.get("error"), "duration_ms": test.get("duration_ms")},
            )
        ]
        if trace_artifact_id:
            evidence.append(
                EvidenceSpec(
                    "http_sample",
                    {"run_id": run_id, "artifact_id": trace_artifact_id},
                    "Playwright trace, screenshot and video for the failing test",
                )
            )
        if status == "flaky":
            out.append(
                Detected(
                    "flaky_test",
                    "low",
                    f"flaky:{test.get('file')}:{test.get('title')}",
                    f"Flaky browser test: {test.get('title')}",
                    {"test": test.get("title")},
                    wf_key,
                    evidence,
                )
            )
        else:
            out.append(
                Detected(
                    "functional_failure",
                    sev.get(criticality.get(wf_key or "", "normal"), "medium"),
                    f"func:{test.get('file')}:{test.get('title')}",
                    f"Browser workflow failed: {test.get('title')}",
                    {"test": test.get("title"), "error": (test.get("error") or "")[:300]},
                    wf_key,
                    evidence,
                )
            )
    if k6_summary:
        for name, passes, fails in _checks(k6_summary.get("root_group") or {}):
            if fails <= 0 or "/" not in name:
                continue
            wf_key, rest = name.split("/", 1)
            step = rest.rsplit(" ", 1)[0]
            out.append(
                Detected(
                    "functional_failure",
                    sev.get(criticality.get(wf_key, "normal"), "medium"),
                    f"func:api:{wf_key}/{step}:{rest.rsplit(' ', 1)[-1]}",
                    f"API check failed: {name}",
                    {"check": name, "passes": passes, "fails": fails},
                    wf_key,
                    [
                        EvidenceSpec(
                            "assertion",
                            {"run_id": run_id, "check": name},
                            f"check '{name}' failed {fails} of {passes + fails} times",
                            {"passes": passes, "fails": fails},
                        )
                    ],
                )
            )
    return out


def detect_probes(run_id: str, summary: dict[str, Any]) -> list[Detected]:
    out: list[Detected] = []
    for name, result in (summary.get("probes") or {}).items():
        if result.get("passed"):
            continue
        wf_key, inv = name.split("/", 1)
        out.append(
            Detected(
                "invariant_violation",
                "critical",
                f"invariant:{name}",
                f"Race condition: invariant '{inv}' violated under concurrent requests ({wf_key})",
                {
                    "invariant": inv,
                    "successes": result.get("successes"),
                    "failed_thresholds": result.get("failed_thresholds"),
                },
                wf_key,
                [
                    EvidenceSpec(
                        "experiment",
                        {"run_id": run_id, "probe": name},
                        f"{result.get('successes')} parallel requests succeeded; failed checks: "
                        f"{', '.join(result.get('failed_thresholds') or []) or 'invariant check'}",
                        result,
                    )
                ],
            )
        )
    return out
