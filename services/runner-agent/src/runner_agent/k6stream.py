"""Incremental aggregation of k6 ``--out json`` output.

Produces, while the test runs:
* live progress windows (FR-15) — VUs, RPS, error rate, p50/p95/p99 over the last window;
* a rolling error-rate watchdog for abort conditions (FR-17);
* 5-second time-series buckets per scope (overall + endpoint) with mergeable log-scale
  latency histograms, so steady-state percentiles can be computed after the run without
  shipping the (large) raw output;
* bounded request samples (errors, slowest, random) with trace ids for evidence.
"""

from __future__ import annotations

import json
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

BUCKET_S = 5
GROWTH = 1.05
_LOG_GROWTH = math.log(GROWTH)


def hist_index(ms: float) -> int:
    return 0 if ms <= 0.1 else int(math.log(ms / 0.1) / _LOG_GROWTH) + 1


def hist_value(index: int) -> float:
    return 0.1 if index == 0 else 0.1 * GROWTH ** (index - 0.5)


def percentile(hist: dict[int, int], q: float) -> float | None:
    total = sum(hist.values())
    if total == 0:
        return None
    rank = q * total
    seen = 0
    for idx in sorted(hist):
        seen += hist[idx]
        if seen >= rank:
            return round(hist_value(idx), 2)
    return round(hist_value(max(hist)), 2)


@dataclass
class Bucket:
    count: int = 0
    errors: int = 0
    hist: dict[int, int] = field(default_factory=dict)

    def add(self, ms: float | None, failed: bool | None) -> None:
        if ms is not None:
            self.count += 1
            i = hist_index(ms)
            self.hist[i] = self.hist.get(i, 0) + 1
        if failed:
            self.errors += 1

    def to_json(self) -> dict[str, Any]:
        return {"count": self.count, "errors": self.errors, "hist": {str(k): v for k, v in self.hist.items()}}


class K6Aggregator:
    def __init__(
        self,
        abort_error_rate: float | None = None,
        abort_window_s: int = 30,
        sample_limit: int = 1000,
        seed: int | None = None,
    ) -> None:
        self.t0: float | None = None
        self.buckets: dict[str, dict[int, Bucket]] = {}
        self.vus: dict[int, int] = {}
        self.window: deque[tuple[float, float | None, bool | None]] = deque()
        self.latest_t = 0.0
        self.abort_error_rate = abort_error_rate
        self.abort_window_s = abort_window_s
        self._over_since: float | None = None
        self.abort_reason: str | None = None
        self.errors_samples: list[dict[str, Any]] = []
        self.slowest: list[tuple[float, dict[str, Any]]] = []
        self.random_samples: list[dict[str, Any]] = []
        self._seen = 0
        self._sample_limit = sample_limit
        self._rng = random.Random(seed)
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _ts(value: str) -> float:
        """Parse k6 RFC 3339 timestamps with nanosecond precision (always UTC)."""
        v = value.replace("Z", "").split("+")[0]
        if "." in v:
            head, frac = v.split(".", 1)
            v = f"{head}.{(frac[:6]).ljust(6, '0')}"
        return datetime.fromisoformat(v).replace(tzinfo=UTC).timestamp()

    def feed_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if obj.get("type") != "Point":
            return
        metric = obj.get("metric")
        data = obj.get("data") or {}
        try:
            t = self._ts(data["time"])
        except (KeyError, ValueError):
            return
        if self.t0 is None:
            self.t0 = t
        self.latest_t = max(self.latest_t, t)
        rel = t - self.t0
        b = int(rel // BUCKET_S)
        tags = data.get("tags") or {}
        value = data.get("value")
        if metric == "vus":
            self.vus[b] = max(self.vus.get(b, 0), int(value or 0))
            return
        if metric not in ("http_req_duration", "http_req_failed"):
            return
        endpoint = tags.get("endpoint") or f"{tags.get('method', '?')} {tags.get('name', '?')}"
        ms = float(value) if metric == "http_req_duration" else None
        failed = bool(value) if metric == "http_req_failed" else None
        for scope in ("overall", f"endpoint:{endpoint}"):
            self.buckets.setdefault(scope, {}).setdefault(b, Bucket()).add(ms, failed)
        self.window.append((t, ms, failed))
        self._sample(metric, t, ms, failed, tags, endpoint)
        self._check_abort(t)

    def _sample(
        self, metric: str, t: float, ms: float | None, failed: bool | None, tags: dict[str, Any], endpoint: str
    ) -> None:
        key = (tags.get("trace_id") or "", endpoint)
        rec = self._pending.setdefault(
            key,
            {
                "t": round(t - (self.t0 or t), 3),
                "endpoint": endpoint,
                "status": tags.get("status"),
                "trace_id": tags.get("trace_id"),
                "workflow": tags.get("workflow"),
                "step": tags.get("step"),
                "error": tags.get("error"),
            },
        )
        if ms is not None:
            rec["duration_ms"] = round(ms, 2)
        if failed is not None:
            rec["failed"] = failed
        if "duration_ms" not in rec or "failed" not in rec:
            if len(self._pending) > 10000:
                self._pending.clear()
            return
        del self._pending[key]
        self._seen += 1
        if rec["failed"] and len(self.errors_samples) < 500:
            self.errors_samples.append(rec)
        if len(self.slowest) < 200 or rec["duration_ms"] > self.slowest[0][0]:
            self.slowest.append((rec["duration_ms"], rec))
            self.slowest.sort(key=lambda x: x[0])
            if len(self.slowest) > 200:
                self.slowest.pop(0)
        if len(self.random_samples) < self._sample_limit:
            self.random_samples.append(rec)
        elif self._rng.random() < self._sample_limit / self._seen:
            self.random_samples[self._rng.randrange(self._sample_limit)] = rec

    def _check_abort(self, now: float) -> None:
        horizon = now - self.abort_window_s
        while self.window and self.window[0][0] < horizon:
            self.window.popleft()
        if self.abort_error_rate is None or self.abort_reason:
            return
        fails = [f for _, _, f in self.window if f is not None]
        if len(fails) < 50:
            self._over_since = None
            return
        rate = sum(fails) / len(fails)
        if rate > self.abort_error_rate:
            self._over_since = self._over_since or now
            if now - self._over_since >= self.abort_window_s:
                self.abort_reason = (
                    f"error rate {rate:.0%} above {self.abort_error_rate:.0%} for {self.abort_window_s}s"
                )
        else:
            self._over_since = None

    def progress(self, window_s: float = 5.0) -> dict[str, Any]:
        horizon = self.latest_t - window_s
        recent = [(ms, f) for t, ms, f in self.window if t >= horizon]
        durations = [ms for ms, _ in recent if ms is not None]
        fails = [f for _, f in recent if f is not None]
        hist: dict[int, int] = {}
        for ms in durations:
            i = hist_index(ms)
            hist[i] = hist.get(i, 0) + 1
        last_bucket = max(self.vus) if self.vus else 0
        return {
            "t": round(self.latest_t - (self.t0 or self.latest_t), 1),
            "vus": self.vus.get(last_bucket, 0),
            "rps": round(len(durations) / window_s, 1),
            "error_rate": round(sum(fails) / len(fails), 4) if fails else 0.0,
            "p50_ms": percentile(hist, 0.5),
            "p95_ms": percentile(hist, 0.95),
            "p99_ms": percentile(hist, 0.99),
        }

    def vu_minutes(self) -> int:
        return math.ceil(sum(v * BUCKET_S for v in self.vus.values()) / 60) if self.vus else 0

    def timeseries(self) -> dict[str, Any]:
        return {
            "bucket_s": BUCKET_S,
            "histogram": {"base_ms": 0.1, "growth": GROWTH},
            "vus": {str(k): v for k, v in sorted(self.vus.items())},
            "scopes": {
                scope: {str(b): bucket.to_json() for b, bucket in sorted(buckets.items())}
                for scope, buckets in self.buckets.items()
            },
            "samples": {
                "errors": self.errors_samples,
                "slowest": [r for _, r in reversed(self.slowest)],
                "random": self.random_samples,
            },
        }
