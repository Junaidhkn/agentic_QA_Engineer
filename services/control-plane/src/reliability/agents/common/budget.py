"""Per-graph budgets (agent-workflows §8). Token accounting is done by the LLM gateway; the
graph checks wall-clock time and tool calls at node boundaries."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from reliability.llm.types import Budget, BudgetExhausted

DEFAULTS = {
    "discovery": {"tokens": 400_000, "wall_s": 1800, "tool_calls": 200},
    "testgen": {"tokens": 50_000, "wall_s": 3600, "tool_calls": 50},  # tokens are per workflow
    "diagnosis": {"tokens": 300_000, "wall_s": 1200, "tool_calls": 25},
    "repair": {"tokens": 1_500_000, "wall_s": 7200, "tool_calls": 400},
}


@dataclass
class GraphBudget(Budget):
    wall_s: float = 1800
    tool_calls: int = 200
    calls: int = 0
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def for_graph(cls, graph: str, scale: int = 1, spent: int = 0) -> GraphBudget:
        d = DEFAULTS[graph]
        return cls(tokens=d["tokens"] * scale, spent=spent, wall_s=d["wall_s"], tool_calls=d["tool_calls"] * scale)

    def check(self) -> None:
        if self.remaining <= 0:
            raise BudgetExhausted("token budget exhausted")
        if time.monotonic() - self.started > self.wall_s:
            raise BudgetExhausted("wall-clock budget exhausted")
        if self.calls > self.tool_calls:
            raise BudgetExhausted("tool-call budget exhausted")

    def tool_call(self) -> None:
        self.calls += 1
        self.check()
