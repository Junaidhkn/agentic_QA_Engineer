"""Run time-series produced by the runner (5 s buckets with mergeable log-scale latency
histograms). Used for charts, steady-state windows, knee detection and evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Window:
    start_s: float
    end_s: float


@dataclass
class WindowStats:
    requests: int
    errors: int
    duration_s: float
    p50_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0

    @property
    def rps(self) -> float:
        return self.requests / self.duration_s if self.duration_s else 0.0


class TimeSeries:
    def __init__(self, data: dict[str, Any]) -> None:
        self.bucket_s = int(data.get("bucket_s", 5))
        h = data.get("histogram") or {}
        self.base = float(h.get("base_ms", 0.1))
        self.growth = float(h.get("growth", 1.05))
        self.vus = {int(k): int(v) for k, v in (data.get("vus") or {}).items()}
        self.scopes: dict[str, dict[int, dict[str, Any]]] = {
            scope: {int(b): v for b, v in buckets.items()} for scope, buckets in (data.get("scopes") or {}).items()
        }
        self.samples: dict[str, list[dict[str, Any]]] = data.get("samples") or {}

    @property
    def duration_s(self) -> float:
        last = max([*self.vus, *(b for s in self.scopes.values() for b in s)], default=-1)
        return (last + 1) * self.bucket_s

    def _value(self, index: int) -> float:
        return self.base if index == 0 else self.base * self.growth ** (index - 0.5)

    def _percentile(self, hist: dict[int, int], q: float) -> float | None:
        total = sum(hist.values())
        if not total:
            return None
        rank, seen = q * total, 0
        for idx in sorted(hist):
            seen += hist[idx]
            if seen >= rank:
                return round(self._value(idx), 2)
        return round(self._value(max(hist)), 2)

    def stats(self, scope: str, window: Window | None = None) -> WindowStats:
        buckets = self.scopes.get(scope, {})
        lo = 0 if window is None else math.floor(window.start_s / self.bucket_s)
        hi = math.inf if window is None else math.ceil(window.end_s / self.bucket_s) - 1
        hist: dict[int, int] = {}
        requests = errors = 0
        selected = [b for b in buckets if lo <= b <= hi]
        for b in selected:
            entry = buckets[b]
            requests += int(entry.get("count", 0))
            errors += int(entry.get("errors", 0))
            for k, v in (entry.get("hist") or {}).items():
                hist[int(k)] = hist.get(int(k), 0) + int(v)
        duration = (window.end_s - window.start_s) if window else self.duration_s
        return WindowStats(
            requests,
            errors,
            max(duration, self.bucket_s),
            self._percentile(hist, 0.5),
            self._percentile(hist, 0.9),
            self._percentile(hist, 0.95),
            self._percentile(hist, 0.99),
            round(self._value(max(hist)), 2) if hist else None,
        )

    def series(self, scope: str) -> list[dict[str, Any]]:
        """Per-bucket points for charts."""
        buckets = self.scopes.get(scope, {})
        out = []
        for b in sorted(set(buckets) | set(self.vus)):
            entry = buckets.get(b, {})
            hist = {int(k): int(v) for k, v in (entry.get("hist") or {}).items()}
            count = int(entry.get("count", 0))
            out.append(
                {
                    "t": b * self.bucket_s,
                    "vus": self.vus.get(b, 0),
                    "rps": round(count / self.bucket_s, 2),
                    "error_rate": round(int(entry.get("errors", 0)) / count, 4) if count else 0.0,
                    "p50_ms": self._percentile(hist, 0.5),
                    "p95_ms": self._percentile(hist, 0.95),
                    "p99_ms": self._percentile(hist, 0.99),
                }
            )
        return out

    def endpoint_scopes(self) -> list[str]:
        return sorted(s for s in self.scopes if s.startswith("endpoint:"))


def steady_state_windows(stages: list[dict[str, int]], settle_s: int = 30) -> list[Window]:
    """Plateaus of the load profile (a stage whose target equals the previous stage's), skipping
    the first ``settle_s`` seconds of each plateau. Ramps are excluded (design §8)."""
    windows: list[Window] = []
    t, prev = 0, 0
    for stage in stages:
        d, target = int(stage["duration_s"]), int(stage["target"])
        if target > 0 and target == prev and d > settle_s:
            windows.append(Window(t + settle_s, t + d))
        t += d
        prev = target
    return windows


def steady_state(stages: list[dict[str, int]], total_s: float) -> Window:
    """The longest plateau, or the middle 80% of the run when the profile has no plateau."""
    plateaus = steady_state_windows(stages)
    if plateaus:
        return max(plateaus, key=lambda w: w.end_s - w.start_s)
    return Window(total_s * 0.1, total_s * 0.9)


def knee(ts: TimeSeries, stages: list[dict[str, int]], p95_threshold_ms: float) -> dict[str, Any] | None:
    """Stress-test capacity: the first plateau whose p95 exceeds the threshold or whose throughput
    stops growing (< 5% more requests/s than the previous plateau despite more VUs)."""
    previous_rps = None
    plateaus = steady_state_windows(stages, settle_s=10)
    t, prev = 0, 0
    targets = []
    for stage in stages:
        if int(stage["target"]) == prev and int(stage["target"]) > 0:
            targets.append(int(stage["target"]))
        t += int(stage["duration_s"])
        prev = int(stage["target"])
    for window, vus in zip(plateaus, targets, strict=False):
        stats = ts.stats("overall", window)
        if stats.p95_ms is not None and stats.p95_ms > p95_threshold_ms:
            return {
                "vus": vus,
                "reason": f"p95 {stats.p95_ms:.0f} ms crossed {p95_threshold_ms:.0f} ms",
                "rps": round(stats.rps, 1),
            }
        if previous_rps is not None and stats.rps < previous_rps * 1.05:
            return {"vus": vus, "reason": "throughput stopped increasing", "rps": round(stats.rps, 1)}
        previous_rps = stats.rps
    return None
