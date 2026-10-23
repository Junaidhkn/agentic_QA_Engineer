---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [agents, llm, cost, assumed]
supersedes: 
superseded-by:
---

# ADR-0019: Call LLMs through an internal gateway with model tiers and budgets

## Context and problem statement

LLM calls are the dominant variable cost (operations.md §4) and a data-protection boundary
(customer code leaves our infrastructure — threat model TB3). Different steps need
different capability/cost points, models change frequently, and per-org budgets must be
enforced (FR-31). The brief does not name a model provider.

## Decision drivers

- Cost control per org, per graph, per node.
- Swappable models/providers without code changes.
- Redaction and structured-output validation in one place.
- Usage metering for quotas and evaluation.

## Considered options

1. **Internal `llm` module (gateway) used by all graphs: routes by *purpose* to a model tier (S/M/L), enforces token budgets, applies redaction, prompt caching and schema validation, records `llm_usage`; initial provider Anthropic Claude (Haiku/Sonnet/Opus-class tiers) via the official SDK, behind an `LLMClient` port.**
2. Graphs call provider SDKs directly.
3. Third-party LLM proxy product.

## Decision outcome

Chosen option: "**Internal gateway with tiers and budgets**", because budgets, redaction and
metering must be impossible to bypass, and routing by purpose lets us move steps between
models based on eval results. Provider: Anthropic Claude assumed for strong code reasoning
and tool use; enterprise data terms (no training on inputs, zero/limited retention) are a
procurement prerequisite. A second provider adapter can be added for fallback; evals decide.

### Confidence

Assumed (provider and model mapping to be confirmed; the gateway itself is Recommended).

### Consequences

- Good: predictable spend; one place for safety controls; vendor flexibility.
- Bad / trade-off: gateway must track provider features (caching, structured outputs, tool use); one more module to maintain.
- Follow-up: use the `claude-api` skill when implementing (current model ids, prompt caching, structured outputs); price sheet → cost model in Phase 0.

### Confirmation

Lint rule: provider SDK imports only inside `llm/`; budget-exhaustion tests; `llm_usage`
rows for 100% of calls.

## Pros and cons of the options

### Internal gateway
- Good: control, metering, flexibility.
- Bad: build effort.

### Direct SDK calls
- Good: fastest start.
- Bad: budgets and redaction scattered; vendor lock-in in every graph.

### Third-party proxy
- Good: features off the shelf.
- Bad: another processor of customer code; tenancy/budget semantics may not fit.

## More information

agent-workflows.md §8; threat-model.md TB3.
