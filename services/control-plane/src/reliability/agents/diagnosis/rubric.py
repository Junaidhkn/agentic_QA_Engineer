"""Deterministic confidence rubric (ADR-0015). The model's own confidence is never an input.
Weights are versioned; bands: high ≥ 0.75, medium ≥ 0.45, else low."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

RUBRIC_VERSION = "rubric@1"
HIGH, MEDIUM = 0.75, 0.45


@dataclass
class Factors:
    verified_citations: int = 0
    evidence_kinds: list[str] = field(default_factory=list)
    temporal_alignment: bool = False
    experiment: dict[str, Any] | None = None  # {"name": ..., "result": supports|contradicts|inconclusive}
    contradicting: int = 0
    hot_path_code: bool = False
    failed_citations: int = 0
    cap: str | None = None  # e.g. "medium" when code context or server telemetry is unavailable


def score(f: Factors) -> tuple[float, str, dict[str, Any]]:
    s = 0.0
    s += 0.35 * (1 - math.exp(-0.6 * f.verified_citations))  # diminishing returns
    kinds = set(f.evidence_kinds)
    s += 0.06 * min(4, len(kinds))
    if {"code_location"} & kinds and kinds - {"code_location"}:
        s += 0.04  # code plus runtime evidence
    if f.temporal_alignment:
        s += 0.05
    if f.hot_path_code:
        s += 0.08
    if f.experiment:
        s += {"supports": 0.2, "contradicts": -0.35}.get(str(f.experiment.get("result")), 0.0)
    s -= 0.15 * f.contradicting
    s -= 0.03 * min(5, f.failed_citations)
    s = max(0.0, min(1.0, s))
    band = "high" if s >= HIGH else "medium" if s >= MEDIUM else "low"
    if f.cap == "medium" and band == "high":
        band = "medium"
    elif f.cap == "low":
        band = "low"
    return (
        round(s, 3),
        band,
        {
            "verified_citations": f.verified_citations,
            "evidence_kinds": sorted(kinds),
            "temporal_alignment": f.temporal_alignment,
            "experiment": f.experiment,
            "contradicting": f.contradicting,
            "hot_path_code": f.hot_path_code,
            "failed_citations": f.failed_citations,
            "cap": f.cap,
            "score": round(s, 3),
            "rubric_version": RUBRIC_VERSION,
        },
    )
