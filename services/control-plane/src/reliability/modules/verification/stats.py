"""Performance comparison statistics (evaluation.md §6, ADR-0016).

Each run contributes its steady-state latency histogram for a scope (the runner's mergeable
log-scale buckets). The statistic is the relative difference of the medians of per-run p95
between candidate and baseline. Its 95% confidence interval is a BCa bootstrap: requests are
resampled within each run (multinomial over the run's histogram, i.e. stratified by run), and
the acceleration comes from a leave-one-run-out jackknife. Error rates are compared with a
one-sided two-proportion z-test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

ITERATIONS = 10_000
DEFAULT_NOISE_FLOOR_PCT = 5.0
METHOD = "bootstrap_bca_10k"
_N = NormalDist()


@dataclass(frozen=True)
class RunSample:
    """One run's steady-state histogram for a scope: bucket index -> request count."""

    hist: dict[int, int]
    errors: int = 0

    @property
    def requests(self) -> int:
        return sum(self.hist.values())


@dataclass(frozen=True)
class Comparison:
    baseline_value: float
    candidate_value: float
    delta_pct: float
    ci_low_pct: float
    ci_high_pct: float
    noise_floor_pct: float
    verdict: str  # improved | no_change | regressed | inconclusive
    error_rate_worse: bool
    baseline_error_rate: float
    candidate_error_rate: float
    error_rate_better: bool = False
    method: str = METHOD


def _p95_of_counts(counts: np.ndarray, lower: np.ndarray, growth: float, base_ms: float) -> np.ndarray:
    """p95 for each row of ``counts`` (rows = bootstrap replicates, columns = buckets), interpolated
    log-linearly inside the bucket so the estimate isn't quantised to the 5% bucket width."""
    cum = np.cumsum(counts, axis=-1)
    rank = 0.95 * cum[..., -1:]
    idx = np.argmax(cum >= rank, axis=-1)
    in_bucket = np.take_along_axis(counts, idx[..., None], axis=-1)[..., 0]
    before = np.take_along_axis(cum, idx[..., None], axis=-1)[..., 0] - in_bucket
    frac = np.clip((rank[..., 0] - before) / np.where(in_bucket > 0, in_bucket, 1), 0.0, 1.0)
    low = lower[idx]
    return np.where(low > 0, low * growth**frac, frac * base_ms)


def _bounds(keys: list[int], base_ms: float, growth: float) -> np.ndarray:
    """Lower bound of each bucket: bucket 0 is (0, base]; bucket i is [base·g^(i-1), base·g^i)."""
    return np.array([0.0 if k == 0 else base_ms * growth ** (k - 1) for k in keys])


def _run_p95(sample: RunSample, base_ms: float, growth: float) -> float:
    keys = sorted(sample.hist)
    if not keys:
        return math.nan
    counts = np.array([sample.hist[k] for k in keys], dtype=np.float64)
    return float(_p95_of_counts(counts, _bounds(keys, base_ms, growth), growth, base_ms))


def _statistic(base_p95: np.ndarray, cand_p95: np.ndarray) -> np.ndarray:
    b = np.median(base_p95, axis=-1)
    c = np.median(cand_p95, axis=-1)
    return np.where(b > 0, (c - b) / np.where(b > 0, b, 1) * 100.0, 0.0)


def _replicates(
    samples: list[RunSample], base_ms: float, growth: float, rng: np.random.Generator, n: int
) -> np.ndarray:
    """(n, runs) bootstrap p95 values: each run's requests resampled from its own histogram."""
    cols = []
    for s in samples:
        keys = sorted(s.hist)
        counts = np.array([s.hist[k] for k in keys], dtype=np.float64)
        total = int(counts.sum())
        draws = rng.multinomial(total, counts / total, size=n).astype(np.float64)
        cols.append(_p95_of_counts(draws, _bounds(keys, base_ms, growth), growth, base_ms))
    return np.stack(cols, axis=1)


def _bca_interval(theta_hat: float, boot: np.ndarray, jack: np.ndarray, alpha: float = 0.05) -> tuple[float, float]:
    b = len(boot)
    below = (np.sum(boot < theta_hat) + 0.5 * np.sum(boot == theta_hat)) / b
    below = min(max(below, 1.0 / (b + 1)), b / (b + 1))
    z0 = _N.inv_cdf(below)
    if len(jack) >= 3:
        d = jack.mean() - jack
        denom = 6.0 * float(np.sum(d**2)) ** 1.5
        a = float(np.sum(d**3)) / denom if denom > 0 else 0.0
    else:
        a = 0.0
    out = []
    for q in (alpha / 2, 1 - alpha / 2):
        z = _N.inv_cdf(q)
        adj = _N.cdf(z0 + (z0 + z) / (1 - a * (z0 + z)))
        out.append(float(np.quantile(boot, min(max(adj, 0.0), 1.0))))
    return out[0], out[1]


def _error_rates(base: list[RunSample], cand: list[RunSample]) -> tuple[float, float, float]:
    """(baseline rate, candidate rate, z) — two-proportion z statistic, positive when worse."""
    nb, nc = sum(s.requests for s in base), sum(s.requests for s in cand)
    eb, ec = sum(s.errors for s in base), sum(s.errors for s in cand)
    pb, pc = (eb / nb if nb else 0.0), (ec / nc if nc else 0.0)
    if not nb or not nc:
        return pb, pc, 0.0
    pooled = (eb + ec) / (nb + nc)
    se = math.sqrt(pooled * (1 - pooled) * (1 / nb + 1 / nc))
    if se == 0:
        return pb, pc, 0.0
    return pb, pc, (pc - pb) / se


Z_ONE_SIDED_05 = 1.645


def verdict(ci_low: float, ci_high: float, noise_floor: float) -> str:
    if ci_high < -noise_floor:
        return "improved"
    if ci_low > noise_floor:
        return "regressed"
    if -noise_floor <= ci_low and ci_high <= noise_floor:
        return "no_change"
    return "inconclusive"


def compare(
    baseline: list[RunSample],
    candidate: list[RunSample],
    *,
    noise_floor_pct: float = DEFAULT_NOISE_FLOOR_PCT,
    base_ms: float = 0.1,
    growth: float = 1.05,
    iterations: int = ITERATIONS,
    seed: int = 0,
) -> Comparison:
    """Compare candidate runs against baseline runs for one scope (lower latency is better)."""
    baseline = [s for s in baseline if s.requests]
    candidate = [s for s in candidate if s.requests]
    if not baseline or not candidate:
        raise ValueError("both sides need at least one run with requests")
    b_p95 = np.array([_run_p95(s, base_ms, growth) for s in baseline])
    c_p95 = np.array([_run_p95(s, base_ms, growth) for s in candidate])
    theta = float(_statistic(b_p95, c_p95))
    rng = np.random.default_rng(seed)
    boot = _statistic(
        _replicates(baseline, base_ms, growth, rng, iterations),
        _replicates(candidate, base_ms, growth, rng, iterations),
    )
    jack = []
    for i in range(len(baseline)):
        if len(baseline) > 1:
            jack.append(float(_statistic(np.delete(b_p95, i), c_p95)))
    for j in range(len(candidate)):
        if len(candidate) > 1:
            jack.append(float(_statistic(b_p95, np.delete(c_p95, j))))
    low, high = _bca_interval(theta, np.asarray(boot), np.asarray(jack))
    pb, pc, z = _error_rates(baseline, candidate)
    return Comparison(
        baseline_value=round(float(np.median(b_p95)), 3),
        candidate_value=round(float(np.median(c_p95)), 3),
        delta_pct=round(theta, 2),
        ci_low_pct=round(low, 2),
        ci_high_pct=round(high, 2),
        noise_floor_pct=noise_floor_pct,
        verdict=verdict(low, high, noise_floor_pct),
        error_rate_worse=z > Z_ONE_SIDED_05,
        baseline_error_rate=round(pb, 5),
        candidate_error_rate=round(pc, 5),
        error_rate_better=z < -Z_ONE_SIDED_05,
    )


def noise_floor_from(comparison: Comparison, minimum: float = 2.0) -> float:
    """An A/A comparison's CI half-width becomes the project's noise floor (never below 2%)."""
    return round(max(minimum, (comparison.ci_high_pct - comparison.ci_low_pct) / 2), 2)
