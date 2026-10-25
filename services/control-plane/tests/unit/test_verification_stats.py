"""Comparison statistics (evaluation.md §6): A/A comparisons yield no_change, real changes are
detected, error-rate regressions are flagged, and the BCa interval covers the estimate."""

from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from reliability.modules.verification.stats import RunSample, compare, noise_floor_from, verdict

GROWTH, BASE = 1.05, 0.1


def _run(rng: np.random.Generator, median_ms: float, n: int, sigma: float = 0.35, error_rate: float = 0.0) -> RunSample:
    lat = rng.lognormal(math.log(median_ms), sigma, n)
    idx = np.where(lat <= BASE, 0, np.floor(np.log(lat / BASE) / math.log(GROWTH)).astype(int) + 1)
    keys, counts = np.unique(idx, return_counts=True)
    return RunSample({int(k): int(c) for k, c in zip(keys, counts, strict=True)}, errors=int(n * error_rate))


def _side(seed: int, median: float, runs: int = 3, n: int = 3000, **kw: float) -> list[RunSample]:
    rng = np.random.default_rng(seed)
    return [_run(rng, median, n, **kw) for _ in range(runs)]


@settings(max_examples=12, deadline=None)
@given(seed=st.integers(0, 10_000), median=st.floats(5, 500))
def test_a_a_comparisons_never_claim_a_change(seed: int, median: float) -> None:
    c = compare(_side(seed, median), _side(seed + 1, median), iterations=2000, seed=seed)
    assert c.verdict in ("no_change", "inconclusive"), c  # never improved / regressed
    assert c.ci_low_pct <= c.delta_pct <= c.ci_high_pct
    assert not c.error_rate_worse


@settings(max_examples=5, deadline=None)
@given(seed=st.integers(0, 10_000))
def test_a_a_with_enough_requests_is_no_change(seed: int) -> None:
    c = compare(_side(seed, 120, n=30_000), _side(seed + 1, 120, n=30_000), iterations=1000, seed=seed)
    assert c.verdict == "no_change", c


@settings(max_examples=8, deadline=None)
@given(seed=st.integers(0, 10_000), improvement=st.floats(0.25, 0.6))
def test_real_improvements_and_regressions_are_detected(seed: int, improvement: float) -> None:
    base = _side(seed, 100)
    better = _side(seed + 7, 100 * (1 - improvement))
    up = compare(base, better, iterations=2000, seed=seed)
    assert up.verdict == "improved" and up.ci_high_pct < -5, up
    down = compare(better, base, iterations=2000, seed=seed)
    assert down.verdict == "regressed" and down.ci_low_pct > 5, down


def test_small_samples_are_inconclusive_not_improved() -> None:
    noisy = compare(_side(1, 100, n=40, sigma=0.9), _side(2, 92, n=40, sigma=0.9), iterations=2000)
    assert noisy.verdict in ("inconclusive", "no_change")
    assert noisy.ci_high_pct - noisy.ci_low_pct > 10


def test_error_rate_regression_is_flagged() -> None:
    c = compare(_side(3, 100, error_rate=0.0), _side(4, 60, error_rate=0.05), iterations=1000)
    assert c.verdict == "improved" and c.error_rate_worse and c.candidate_error_rate > c.baseline_error_rate


def test_verdict_bands_and_noise_floor() -> None:
    assert verdict(-30, -10, 5) == "improved"
    assert verdict(10, 30, 5) == "regressed"
    assert verdict(-4, 4, 5) == "no_change"
    assert verdict(-12, 2, 5) == "inconclusive"
    c = compare(_side(5, 80), _side(6, 80), iterations=1000)
    assert 2.0 <= noise_floor_from(c) <= 10
