"""Beta-exit SLO report (benchmarks/harness/slo_report.py) against a fake Prometheus API."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx

HARNESS = Path(__file__).resolve().parents[4] / "benchmarks" / "harness" / "slo_report.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("slo_report", HARNESS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["slo_report"] = mod
    spec.loader.exec_module(mod)
    return mod


slo = _module()
HEALTHY = {
    "re_http_requests_total{status": 0.9995,
    "re_http_request_duration_seconds_bucket": 0.97,
    "re_job_start_seconds_bucket": 0.985,
    "re_kill_latency_seconds_count": 0.0,
    'check="canary"': 0.997,
    "re_integrity_violations": 0.0,
    'check="restore_drill"': 2.5,
    "re_rls_denials_total": 0.0,
}


def _client(values: dict[str, float | None], seen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        seen.append(q)
        assert request.headers["X-Scope-OrgID"] == "platform"
        value = next((v for k, v in values.items() if k in q), None)
        result = [] if value is None else [{"metric": {}, "value": [1761300000, str(value)]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})

    return httpx.Client(
        base_url="http://mimir/prometheus",
        transport=httpx.MockTransport(handler),
        headers={"X-Scope-OrgID": "platform"},
    )


def test_healthy_month_passes() -> None:
    seen: list[str] = []
    rep = slo.report(_client(HEALTHY, seen), 30)
    assert rep["passed"] and len(seen) == len(slo.CRITERIA)
    assert all("[30d" in q for q in seen if "time()" not in q)
    md = slo.markdown(rep)
    assert (
        "**PASS**" in md
        and "99.950 %" in md
        and "| Days since the last successful restore drill | ≤ 8 | 2.5 | ✅ |" in md
    )


def test_any_breach_or_missing_data_fails() -> None:
    bad = {**HEALTHY, "re_job_start_seconds_bucket": 0.93, "re_kill_latency_seconds_count": 1.0}
    rep = slo.report(_client(bad, []), 30)
    failed = {r["key"] for r in rep["criteria"] if not r["passed"]}
    assert failed == {"job_start", "kill_latency"} and not rep["passed"]

    no_canary = {k: v for k, v in HEALTHY.items() if k != 'check="canary"'}
    rep = slo.report(_client(no_canary, []), 30)
    canary = next(r for r in rep["criteria"] if r["key"] == "canary")
    assert canary["display"] == "no data" and not canary["passed"] and not rep["passed"]

    nan = {**HEALTHY, "re_http_requests_total{status": float("nan")}
    assert not slo.report(_client(nan, []), 30)["criteria"][0]["passed"]
