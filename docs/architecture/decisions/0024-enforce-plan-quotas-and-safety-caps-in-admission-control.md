---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [safety, cost, quotas]
supersedes: 
superseded-by:
---

# ADR-0024: Enforce plan quotas and safety caps in a single admission-control step

## Context and problem statement

Load runs, sandbox minutes and LLM tokens cost real money and can harm third parties. The
brief requires rate limits and cost efficiency; FR-14 and FR-31 require admission control
and quotas. Checks scattered across endpoints and workers drift and leave gaps.

## Decision drivers

- One place that decides whether work may start.
- Atomic quota reservation under concurrency.
- Defense in depth with runner-side limits.

## Considered options

1. **`admission` module invoked for every run, discovery, suite generation, diagnosis and repair: role → target/environment policy → plan caps → quota reservation (ledger, advisory lock) → concurrency limits → approval requirement; reservations committed/released at completion; runner enforces wall-clock and resource limits from the lease independently; per-principal rate limits in Redis.**
2. Per-endpoint checks plus monthly usage reconciliation.
3. Billing-provider-metered usage only.

## Decision outcome

Chosen option: "**Single admission step with reservations**", because a single ordered
decision function is testable (property tests), produces stable error codes for the UI and
CI clients, and reservations prevent concurrent requests from overspending. Runner-side
limits ensure that even an admission bug can't produce unbounded load. Operators have a
global and per-org kill switch (suspend org → admission rejects and running leases cancel).

### Confidence

Recommended.

### Consequences

- Good: predictable costs; consistent UX; abuse containment.
- Bad / trade-off: admission is on the critical path for run creation (fail closed when Redis/DB unavailable).
- Follow-up: plan table values set in Phase 0 with pricing; Stripe metering Deferred.

### Confirmation

Unit/property tests for admission ordering and reservation arithmetic; concurrency test
(parallel run creations cannot exceed quota).

## Pros and cons of the options

### Single admission + reservations
- Good: consistent, safe.
- Bad: central dependency.

### Scattered checks
- Good: local simplicity.
- Bad: gaps, races.

### Billing-only metering
- Good: commercial accuracy.
- Bad: after-the-fact; no safety.

## More information

low-level-design.md §4; data-model.md §5.
