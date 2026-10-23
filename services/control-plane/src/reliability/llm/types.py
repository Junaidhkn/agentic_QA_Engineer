"""LLM gateway types (ADR-0019). Provider-agnostic; no dependency on domain modules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Tier = Literal["S", "M", "L"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class PurposeConfig:
    tier: Tier
    effort: Effort
    max_tokens: int


# Routing by purpose: change models/effort here (or via settings), never in graph code.
PURPOSES: dict[str, PurposeConfig] = {
    "discovery.synthesize": PurposeConfig("M", "medium", 32000),
    "discovery.criticality": PurposeConfig("S", "low", 4000),
    "discovery.explore": PurposeConfig("M", "low", 4000),
    "testgen.repair": PurposeConfig("M", "medium", 16000),
    "diagnosis.triage": PurposeConfig("S", "low", 4000),
    "diagnosis.investigate": PurposeConfig("L", "high", 16000),
    "diagnosis.hypothesize": PurposeConfig("L", "high", 32000),
    "repair.plan": PurposeConfig("L", "high", 16000),
    "repair.code": PurposeConfig("L", "high", 64000),
}


@dataclass
class Budget:
    """Token budget for one graph run. Raises BudgetExhausted when spent."""

    tokens: int
    spent: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.tokens - self.spent)

    def charge(self, tokens: int) -> None:
        self.spent += tokens


class BudgetExhausted(Exception):
    pass


class LLMRefusal(Exception):
    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"model declined ({category or 'unspecified'})")
        self.category, self.explanation = category, explanation


class LLMUnavailable(Exception):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class LLMRequest:
    purpose: str
    system: str
    messages: list[dict[str, Any]]
    schema: dict[str, Any] | None = None  # JSON Schema the gateway validates output against
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int | None = None
    # Constrained decoding (structured outputs) needs a closed schema (additionalProperties:
    # false everywhere, supported keywords only). Open schemas such as Workflow IR set this to
    # False: the schema then goes in the prompt and is enforced by validation + one re-ask.
    constrained: bool = True


@dataclass
class LLMResponse:
    model: str
    content: list[dict[str, Any]]  # provider-neutral blocks: {"type": "text"|"tool_use", ...}
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    latency_ms: int = 0
    served_by_fallback: bool = False

    @property
    def text(self) -> str:
        return "".join(b["text"] for b in self.content if b["type"] == "text")

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b["type"] == "tool_use"]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UsageRecord:
    org_id: uuid.UUID
    purpose: str
    subject_id: uuid.UUID | None
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    outcome: str


class UsageSink(Protocol):
    """Implemented by the calling module: meters usage (llm_usage rows, org quota ledger)."""

    async def record(self, record: UsageRecord) -> None: ...
    async def allow(self, org_id: uuid.UUID, estimated_tokens: int) -> bool: ...
