"""Phase 4: time-series math and deterministic detectors."""

from __future__ import annotations

import math

from reliability.modules.findings.detectors import ScopeRow, detect_functional, detect_load, detect_probes
from reliability.modules.runs.timeseries import TimeSeries, Window, knee, steady_state, steady_state_windows


def _hist(values: list[float]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ms in values:
        i = 0 if ms <= 0.1 else int(math.log(ms / 0.1) / math.log(1.05)) + 1
        out[str(i)] = out.get(str(i), 0) + 1
    return out


def make_ts(
    buckets: dict[int, tuple[list[float], int]], vus: dict[int, int], endpoint: str = "POST /api/orders"
) -> TimeSeries:
    scope = {str(b): {"count": len(v), "errors": e, "hist": _hist(v)} for b, (v, e) in buckets.items()}
    return TimeSeries(
        {
            "bucket_s": 5,
            "histogram": {"base_ms": 0.1, "growth": 1.05},
            "vus": {str(k): v for k, v in vus.items()},
            "scopes": {"overall": scope, f"endpoint:{endpoint}": scope},
            "samples": {
                "slowest": [
                    {"endpoint": endpoint, "duration_ms": 900, "trace_id": "ab" * 16, "status": "200", "t": 12}
                ],
                "errors": [{"endpoint": endpoint, "duration_ms": 30, "status": "500", "t": 40, "trace_id": "cd" * 16}],
            },
        }
    )


def test_steady_state_excludes_ramps() -> None:
    stages = [{"duration_s": 120, "target": 1000}, {"duration_s": 600, "target": 1000}, {"duration_s": 60, "target": 0}]
    assert steady_state_windows(stages) == [Window(150, 720)]
    assert steady_state([{"duration_s": 100, "target": 5}], 100) == Window(10, 90)


def test_window_percentiles_merge_histograms() -> None:
    ts = make_ts({b: ([float(x) for x in range(1, 101)], 0) for b in range(10)}, {b: 10 for b in range(10)})
    st = ts.stats("overall", Window(0, 50))
    assert st.requests == 1000 and abs(st.p95_ms - 95) / 95 < 0.05
    assert abs(st.rps - 20) < 0.01
    assert len(ts.series("overall")) == 10


def test_knee_detects_latency_crossing() -> None:
    stages = [
        {"duration_s": 20, "target": 10},
        {"duration_s": 20, "target": 10},
        {"duration_s": 20, "target": 20},
        {"duration_s": 20, "target": 20},
    ]
    fast = ([50.0] * 100, 0)
    slow = ([900.0] * 100, 0)
    ts = make_ts({**{b: fast for b in range(8)}, **{b: slow for b in range(8, 16)}}, {b: 10 for b in range(16)})
    result = knee(ts, stages, 500)
    assert result is not None and result["vus"] == 20 and "crossed" in result["reason"]


def test_load_detector_slo_breach_regression_and_evidence() -> None:
    ts = make_ts({b: ([600.0] * 50, 0) for b in range(12)}, {b: 50 for b in range(12)})
    rows = {
        "overall": ScopeRow("overall", 600, 10, 0.0, 400, 600, 700),
        "endpoint:POST /api/orders": ScopeRow("endpoint:POST /api/orders", 600, 10, 0.0, 400, 1200, 1500),
    }
    found = detect_load(
        "r1",
        rows,
        {"p95_ms": 500, "error_rate": 0.01},
        ts,
        Window(0, 60),
        {"endpoint:POST /api/orders": [300.0, 310.0, 290.0]},
    )
    by_fp = {f.fingerprint: f for f in found}
    breach = by_fp["slo:endpoint:POST /api/orders:p95"]
    assert breach.severity == "high" and breach.headline["at_vus"] == 50
    assert {e.kind for e in breach.evidence} == {"metric_window", "http_sample"}
    assert "slo:overall:p95" not in by_fp  # endpoint-level finding is more specific
    regression = by_fp["regression:endpoint:POST /api/orders:p95"]
    assert regression.headline["delta_pct"] > 100


def test_no_regression_within_noise() -> None:
    rows = {"endpoint:GET /a": ScopeRow("endpoint:GET /a", 100, 5, 0, 100, 330, 400)}
    found = detect_load("r", rows, {"p95_ms": 1000}, None, None, {"endpoint:GET /a": [300.0, 320.0, 310.0]})
    assert not found


def test_error_spike_detected_but_not_sustained_failure() -> None:
    normal = ([10.0] * 100, 0)
    spiky = {b: normal for b in range(12)}
    spiky[6] = ([10.0] * 100, 40)
    spiky[7] = ([10.0] * 100, 50)
    found = detect_load("r", {}, {"p95_ms": 500}, make_ts(spiky, {b: 5 for b in range(12)}), None, {})
    assert [f.type for f in found] == ["error_spike"]
    sustained = {b: ([10.0] * 100, 60) for b in range(12)}
    assert not [f for f in detect_load("r", {}, {}, make_ts(sustained, {}), None, {}) if f.type == "error_spike"]


def test_functional_and_probe_detectors() -> None:
    summary = {
        "tests": [
            {
                "file": "tests/wf_checkout_place_order.spec.ts",
                "title": "Checkout",
                "status": "unexpected",
                "error": "expect(locator).toBeVisible() failed",
            },
            {"file": "tests/wf_search.spec.ts", "title": "Search", "status": "flaky"},
        ]
    }
    k6 = {"root_group": {"groups": [{"checks": [{"name": "login.basic/submit status", "passes": 2, "fails": 1}]}]}}
    found = detect_functional("r", summary, k6, {"checkout.place_order": "critical", "login.basic": "high"}, "art1")
    kinds = {(f.type, f.severity) for f in found}
    assert ("functional_failure", "critical") in kinds and ("flaky_test", "low") in kinds
    assert ("functional_failure", "high") in kinds
    probes = detect_probes("r", {"probes": {"checkout/stock": {"passed": False, "successes": 10}}})
    assert probes[0].type == "invariant_violation" and probes[0].severity == "critical"
