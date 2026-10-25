"""Compile a load profile into k6 ``options.json`` (FR-11, FR-16, FR-17).

* one k6 scenario per workflow in the workload mix, VUs/rates split by weight;
* SLO thresholds decide pass/fail; per-endpoint and per-workflow submetrics are added with
  always-true thresholds so they appear in the summary without affecting the verdict;
* an abort threshold stops runs whose error rate explodes (the runner also applies a
  rolling-window abort, which is more precise than k6's cumulative rate).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from reliability.platform.errors import AppError

TREND_STATS = ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max", "count"]

TEMPLATES: dict[str, dict[str, Any]] = {
    "smoke": {"executor": "ramping-vus", "stages": [(30, 1.0)], "fraction": None, "vus": 2},
    "baseline": {"executor": "ramping-vus", "stages": [(30, 1.0), (120, 1.0)], "fraction": 0.1},
    "load": {"executor": "ramping-vus", "stages": [(120, 1.0), (600, 1.0), (60, 0.0)], "fraction": 1.0},
    "stress": {
        "executor": "ramping-vus",
        "stages": [
            (60, 0.25),
            (120, 0.25),
            (60, 0.5),
            (120, 0.5),
            (60, 0.75),
            (120, 0.75),
            (60, 1.0),
            (120, 1.0),
            (30, 0.0),
        ],
        "fraction": 1.0,
    },
    "spike": {
        "executor": "ramping-vus",
        "stages": [(60, 0.1), (10, 1.0), (60, 1.0), (10, 0.1), (60, 0.1)],
        "fraction": 1.0,
    },
    "soak": {"executor": "ramping-vus", "stages": [(120, 1.0), (3300, 1.0), (60, 0.0)], "fraction": 0.6},
}


@dataclass(frozen=True)
class Stage:
    duration_s: int
    target: int


def template_stages(profile_type: str, max_vus: int) -> list[dict[str, int]]:
    t = TEMPLATES[profile_type]
    peak = t.get("vus") or max(1, round(max_vus * (t["fraction"] or 1.0)))
    return [{"duration_s": d, "target": max(0 if f == 0 else 1, round(peak * f))} for d, f in t["stages"]]


def validate_profile(
    profile_type: str, executor: str, stages: list[dict[str, int]], max_vus: int, duration_s: int
) -> None:
    if not stages:
        raise AppError("VALIDATION_FAILED", "a profile needs at least one stage")
    total = sum(int(s["duration_s"]) for s in stages)
    if total != duration_s:
        raise AppError(
            "VALIDATION_FAILED",
            "duration_s must equal the sum of stage durations",
            {"sum_of_stages": total, "duration_s": duration_s},
        )
    if executor == "ramping-vus" and max(int(s["target"]) for s in stages) > max_vus:
        raise AppError("VALIDATION_FAILED", "a stage target exceeds max_vus")
    if any(int(s["duration_s"]) <= 0 or int(s["target"]) < 0 for s in stages):
        raise AppError("VALIDATION_FAILED", "stage durations must be > 0 and targets >= 0")
    if profile_type == "smoke" and max_vus > 10:
        raise AppError("VALIDATION_FAILED", "smoke profiles use at most 10 VUs")


def _split(n: float, weight: float) -> int:
    return max(1, math.ceil(n * weight)) if n > 0 else 0


def compile_options(
    *,
    executor: str,
    stages: list[dict[str, int]],
    max_vus: int,
    workload_mix: dict[str, float],
    exec_names: dict[str, str],
    endpoints: dict[str, list[str]],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    mix = {k: w for k, w in workload_mix.items() if k in exec_names and w > 0}
    if not mix:
        raise AppError(
            "VALIDATION_FAILED", "the workload mix has no runnable API workflows", {"available": sorted(exec_names)}
        )
    total_weight = sum(mix.values())
    scenarios: dict[str, Any] = {}
    for key, weight in sorted(mix.items()):
        share = weight / total_weight
        name = exec_names[key]
        if executor == "ramping-vus":
            scenarios[name] = {
                "executor": "ramping-vus",
                "exec": name,
                "startVUs": 0,
                "gracefulRampDown": "10s",
                "stages": [{"duration": f"{s['duration_s']}s", "target": _split(s["target"], share)} for s in stages],
            }
        else:
            vus = _split(max_vus, share)
            base = {"exec": name, "timeUnit": "1s", "preAllocatedVUs": max(1, vus // 2), "maxVUs": vus}
            if executor == "ramping-arrival-rate":
                scenarios[name] = {
                    "executor": executor,
                    "startRate": 0,
                    **base,
                    "stages": [
                        {"duration": f"{s['duration_s']}s", "target": _split(s["target"], share)} for s in stages
                    ],
                }
            else:
                scenarios[name] = {
                    "executor": executor,
                    "rate": _split(stages[0]["target"], share),
                    "duration": f"{sum(s['duration_s'] for s in stages)}s",
                    **base,
                }

    p95 = thresholds.get("p95_ms", 500)
    err = thresholds.get("error_rate", 0.01)
    abort = thresholds.get("abort_error_rate", 0.5)
    k6_thresholds: dict[str, Any] = {
        "http_req_duration{expected_response:true}": [f"p(95)<{p95}"],
        "http_req_failed": [
            f"rate<{err}",
            {"threshold": f"rate<{abort}", "abortOnFail": True, "delayAbortEval": "30s"},
        ],
        "checks": ["rate>=0"],
        "dropped_iterations": ["count>=0"],
    }
    for key in mix:
        k6_thresholds[f"http_req_duration{{workflow:{key}}}"] = ["p(95)>=0"]
        k6_thresholds[f"http_req_failed{{workflow:{key}}}"] = ["rate>=0"]
        for endpoint in endpoints.get(key, []):
            k6_thresholds[f"http_req_duration{{endpoint:{endpoint}}}"] = ["p(95)>=0"]
            k6_thresholds[f"http_req_failed{{endpoint:{endpoint}}}"] = ["rate>=0"]
    return {
        "scenarios": scenarios,
        "thresholds": k6_thresholds,
        "summaryTrendStats": TREND_STATS,
        "discardResponseBodies": False,
        "userAgent": "ReliabilityEngineer-k6/1.0",
        "maxRedirects": 0,
        "systemTags": [
            "status",
            "method",
            "name",
            "group",
            "check",
            "error",
            "error_code",
            "scenario",
            "expected_response",
        ],
    }
