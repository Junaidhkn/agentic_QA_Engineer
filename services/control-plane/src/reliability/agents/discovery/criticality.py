"""Criticality rules (agent-workflows §3): the model may propose a level, but auth, payment and
data-writing workflows are never ranked below ``high``, and payment/checkout/auth flows are
``critical``."""

from __future__ import annotations

import re
from typing import Any

CRITICAL = re.compile(
    r"(checkout|payment|pay|billing|charge|order|purchase|subscribe|login|signin|sign-in|signup|"
    r"sign-up|register|auth|token|password)",
    re.I,
)
RANK = {"normal": 0, "high": 1, "critical": 2}


def floor(ir: dict[str, Any], data_write: bool) -> str:
    text = " ".join(
        [ir["key"], ir.get("title", "")]
        + [
            s.get("request", {}).get("path", "") + " " + s.get("url", "")
            for s in [*ir.get("steps", []), *ir.get("api_equivalent", [])]
        ]
    )
    if CRITICAL.search(text):
        return "critical"
    if data_write or ir.get("preconditions"):
        return "high"
    return "normal"


def apply(ir: dict[str, Any], proposed: str | None, data_write: bool) -> str:
    level = floor(ir, data_write)
    if proposed in RANK and RANK[proposed] > RANK[level]:
        level = proposed
    return level
