---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [testing, load]
supersedes: 
superseded-by:
---

# ADR-0012: Use Playwright for functional tests and protocol-level k6 for load

## Context and problem statement

The brief names Playwright for functional browser testing and Grafana k6 for load, stress,
spike and soak testing at 1,000+ VUs (NFR-1). Real browsers are expensive (~hundreds of MB
RAM and significant CPU each); 1,000 browser VUs would need dozens of machines per run and
measure front-end rendering rather than backend scalability.

## Decision drivers

- Meet 1,000 VUs on one generator at low cost (NFR-1, NFR-11).
- Realistic user journeys in functional tests.
- Valid measurements (generator must not be the bottleneck — FR-19).

## Considered options

1. **Playwright for functional/smoke tests of browser workflows; k6 protocol-level (HTTP) scenarios derived from each workflow's API equivalent for load; single generator ≤ 1,000 VUs; distributed k6 Deferred.**
2. k6 browser module for load (browser VUs).
3. Mixed: protocol-level load plus a few browser VUs measuring user-perceived performance.

## Decision outcome

Chosen option: "**Playwright for function, protocol-level k6 for load**", because protocol
level load reaches 1,000 VUs on a single 4-vCPU generator and directly stresses the backend
paths where vibe-coded apps fail (DB pools, N+1, locks). Workflow IR captures both a
browser form and an API equivalent (ADR-0013) — the API sequence can be recorded from the
browser run's network log when not known statically. Generator health is monitored and
saturated runs are marked invalid. Option 3 is a Deferred enhancement (trigger: customer
demand for front-end performance under load).

Executors: `ramping-arrival-rate` (open model) recommended for capacity/stress tests;
`ramping-vus` (closed model) for "N concurrent users" journeys; profile defines which.

### Confidence

Confirmed (tools from the brief); protocol-level-only load Recommended.

### Consequences

- Good: cheap, scalable, backend-focused load.
- Bad / trade-off: client-side rendering performance under load isn't measured; API equivalents may miss browser-only behaviors (websockets, client-side caching).
- Follow-up: distributed k6 via k6-operator when runs > 1,000 VUs are needed (Deferred with Kubernetes, ADR-0009).

### Confirmation

NFR-1 benchmark (evaluation.md §5); generator-validity checks in every run.

## Pros and cons of the options

### Playwright + protocol k6
- Good: cost-efficient, scalable, realistic backend stress.
- Bad: no browser metrics under load.

### Browser VUs
- Good: end-to-end realism.
- Bad: ~50–100× more compute per VU; unaffordable at 1,000 VUs for target customers.

### Mixed
- Good: both perspectives.
- Bad: added complexity; not needed for MVP problems.

## More information

low-level-design.md §6–8; operations.md §4.
