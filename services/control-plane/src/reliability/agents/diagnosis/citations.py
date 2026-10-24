"""Deterministic citation checks (ADR-0015).

A claim cites one evidence item. The check passes when the evidence exists and belongs to this
diagnosis, every number in the claim matches a number in the evidence (summary or snapshot)
within tolerance — with unit conversion for ms/s and percentages — and, for claims without
numbers, when the claim names something the evidence is about (endpoint, table, file, …).
Code locations must exist at the commit. Only ``verified`` citations count as support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from reliability.agents.diagnosis.code import CodeIndex

REL_TOL = 0.05
ABS_TOL = 1.0
NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(ms|s|%|x|×|k)?(?![\w.])", re.I)
FRACTION = re.compile(r"(\d+)\s*/\s*(\d+)")
STOP = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "for",
    "per",
    "are",
    "was",
    "were",
    "has",
    "have",
    "each",
    "into",
    "when",
    "than",
    "only",
    "under",
    "load",
    "request",
    "requests",
    "query",
    "queries",
    "time",
}


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    summary: str
    locator: dict[str, Any]
    snapshot: dict[str, Any] | None


def _numbers(value: Any, out: list[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        out.append(float(value))
    elif isinstance(value, str):
        out.extend(float(m.group(1)) for m in NUMBER.finditer(value))
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, list):
        for v in value[:500]:
            _numbers(v, out)


def evidence_numbers(ev: Evidence) -> list[float]:
    nums: list[float] = []
    _numbers(ev.summary, nums)
    _numbers(ev.snapshot or {}, nums)
    _numbers(ev.locator, nums)
    return nums


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL if max(abs(a), abs(b)) >= 10 else 0.01, REL_TOL * max(abs(a), abs(b)))


def _candidates(value: float, unit: str | None) -> list[float]:
    unit = (unit or "").lower()
    if unit == "s":
        return [value, value * 1000]
    if unit == "ms":
        return [value, value / 1000]
    if unit == "%":
        return [value, value / 100]
    if unit == "k":
        return [value * 1000]
    return [value]


def claim_numbers(claim: str) -> list[tuple[float, str | None]]:
    text = FRACTION.sub(lambda m: f"{m.group(1)} {m.group(2)}", claim)
    return [(float(m.group(1)), m.group(2)) for m in NUMBER.finditer(text)]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z_][a-z0-9_./{}-]{2,}", text.lower()) if t not in STOP}


def check_claim(claim: str, ev: Evidence | None, allowed: set[str]) -> tuple[str, str]:
    """(check_result, reason) with check_result in verified | unverifiable | failed."""
    if ev is None:
        return "failed", "evidence does not exist"
    if ev.id not in allowed:
        return "failed", "evidence belongs to another finding"
    numbers = claim_numbers(claim)
    if numbers:
        pool = evidence_numbers(ev)
        missing = [
            f"{v:g}{u or ''}" for v, u in numbers if not any(_close(c, p) for c in _candidates(v, u) for p in pool)
        ]
        if missing:
            return "failed", f"numbers not found in evidence: {', '.join(missing[:4])}"
        return "verified", "numbers match the evidence"
    about = _tokens(ev.summary) | _tokens(" ".join(str(v) for v in ev.locator.values()))
    if _tokens(claim) & about:
        return "verified", "claim refers to what the evidence shows"
    return "unverifiable", "claim has no checkable content"


def check_code_location(loc: dict[str, Any], code: CodeIndex | None) -> bool:
    if code is None:
        return False
    path = str(loc.get("path", ""))
    if not code.exists(path):
        return False
    lines = loc.get("lines") or []
    total = code.line_count(path)
    return all(isinstance(n, int) and 1 <= n <= total for n in lines)
