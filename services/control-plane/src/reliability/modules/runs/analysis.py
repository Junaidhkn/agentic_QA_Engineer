"""Turn k6 / Playwright outputs into run summaries and a verdict (deterministic).

k6 ``--summary-export`` semantics (verified against k6 0.54): a threshold value of ``true``
means the threshold FAILED; for ``http_req_failed`` ``passes`` counts failed requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SUBMETRIC = re.compile(r"^(?P<metric>[a-z_]+)\{(?P<tag>[a-z_]+):(?P<value>.+)\}$")
GENERATOR_CPU_LIMIT = 0.85


@dataclass
class ScopeStats:
    requests: int = 0
    error_rate: float = 0.0
    p50_ms: float | None = None
    p90_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    max_ms: float | None = None


@dataclass
class K6Result:
    scopes: dict[str, ScopeStats] = field(default_factory=dict)
    failed_thresholds: list[dict[str, Any]] = field(default_factory=list)
    checks_passed: int = 0
    checks_failed: int = 0
    vus_max: int = 0
    rps: float = 0.0
    dropped_iterations: int = 0


def _trend(stats: dict[str, Any], target: ScopeStats) -> None:
    target.p50_ms = stats.get("med")
    target.p90_ms = stats.get("p(90)")
    target.p95_ms = stats.get("p(95)")
    target.p99_ms = stats.get("p(99)")
    target.max_ms = stats.get("max")
    target.requests = max(target.requests, int(stats.get("count") or 0))


def parse_k6_summary(summary: dict[str, Any]) -> K6Result:
    metrics: dict[str, dict[str, Any]] = summary.get("metrics", {})
    result = K6Result()
    overall = result.scopes.setdefault("overall", ScopeStats())
    for name, data in metrics.items():
        for expr, failed in (data.get("thresholds") or {}).items():
            if failed:
                result.failed_thresholds.append({"metric": name, "threshold": expr, "observed": _observed(data, expr)})
        m = SUBMETRIC.match(name)
        if name == "http_req_duration{expected_response:true}":
            _trend(data, overall)
        elif name == "http_req_failed":
            overall.error_rate = float(data.get("value") or 0)
            overall.requests = int(data.get("passes", 0)) + int(data.get("fails", 0))
        elif name == "http_reqs":
            result.rps = float(data.get("rate") or 0)
        elif name == "checks":
            result.checks_passed, result.checks_failed = int(data.get("passes", 0)), int(data.get("fails", 0))
        elif name == "vus_max":
            result.vus_max = int(data.get("value") or data.get("max") or 0)
        elif name == "dropped_iterations":
            result.dropped_iterations = int(data.get("count") or 0)
        elif m and m["tag"] in ("workflow", "endpoint"):
            scope = result.scopes.setdefault(
                f"{'scenario' if m['tag'] == 'workflow' else 'endpoint'}:{m['value']}", ScopeStats()
            )
            if m["metric"] == "http_req_duration":
                _trend(data, scope)
            elif m["metric"] == "http_req_failed":
                scope.error_rate = float(data.get("value") or 0)
                scope.requests = int(data.get("passes", 0)) + int(data.get("fails", 0))
    return result


def _observed(data: dict[str, Any], expr: str) -> float | None:
    agg = expr.split("<")[0].split(">")[0].split("=")[0].strip()
    if agg.startswith("p(") or agg in ("avg", "min", "max", "med"):
        return data.get(agg)
    if agg == "rate":
        return data.get("value")
    if agg == "count":
        return data.get("count")
    return None


@dataclass
class Verdict:
    status: str  # passed | failed | invalid
    reason: str | None
    summary: dict[str, Any]


def load_verdict(result: K6Result, generator: dict[str, Any], aborted: str | None, duration_s: float) -> Verdict:
    overall = result.scopes.get("overall", ScopeStats())
    summary = {
        "overall": overall.__dict__,
        "rps": round(result.rps, 2),
        "vus_max": result.vus_max,
        "checks": {"passed": result.checks_passed, "failed": result.checks_failed},
        "failed_thresholds": result.failed_thresholds,
        "dropped_iterations": result.dropped_iterations,
        "generator": generator,
        "duration_s": round(duration_s, 1),
    }
    max_cpu = float(generator.get("max_cpu") or 0)
    if max_cpu > GENERATOR_CPU_LIMIT or result.dropped_iterations > 0:
        generator["valid"] = False
        return Verdict(
            "invalid",
            "load generator saturated (cpu "
            f"{max_cpu:.0%}, dropped iterations {result.dropped_iterations}); results are not "
            "attributable to the target",
            summary,
        )
    generator["valid"] = True
    if aborted:
        return Verdict("failed", f"aborted: {aborted}", summary)
    slo_failures = [
        t
        for t in result.failed_thresholds
        if t["metric"] in ("http_req_duration{expected_response:true}", "http_req_failed")
    ]
    if slo_failures or result.checks_failed:
        reasons = [f"{t['metric']} {t['threshold']} (observed {t['observed']})" for t in slo_failures]
        if result.checks_failed:
            reasons.append(f"{result.checks_failed} failed checks")
        return Verdict("failed", "; ".join(reasons), summary)
    return Verdict("passed", None, summary)


def parse_playwright_report(report: dict[str, Any]) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                results = test.get("results", [])
                status = test.get("status")  # expected | unexpected | flaky | skipped
                error = next((r.get("error", {}).get("message") for r in results if r.get("error")), None)
                attachments = [a.get("path") for r in results for a in r.get("attachments", []) if a.get("path")]
                tests.append(
                    {
                        "file": spec.get("file"),
                        "title": spec.get("title"),
                        "status": status,
                        "error": (error or "")[:2000] or None,
                        "attachments": attachments,
                        "duration_ms": sum(r.get("duration", 0) for r in results),
                    }
                )
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)
    counts = {s: sum(1 for t in tests if t["status"] == s) for s in ("expected", "unexpected", "flaky", "skipped")}
    return {"tests": tests, "counts": counts}


def functional_verdict(pw: dict[str, Any] | None, k6: K6Result | None) -> Verdict:
    summary: dict[str, Any] = {}
    failed: list[str] = []
    if pw is not None:
        summary["playwright"] = pw["counts"]
        summary["tests"] = [{k: v for k, v in t.items() if k != "attachments"} for t in pw["tests"]]
        if pw["counts"]["unexpected"]:
            failed.append(f"{pw['counts']['unexpected']} browser test(s) failed")
    if k6 is not None:
        summary["api"] = {
            "checks": {"passed": k6.checks_passed, "failed": k6.checks_failed},
            "overall": k6.scopes.get("overall", ScopeStats()).__dict__,
        }
        if k6.checks_failed:
            failed.append(f"{k6.checks_failed} API check(s) failed")
    return Verdict("failed" if failed else "passed", "; ".join(failed) or None, summary)
