---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [architecture-style, repository]
supersedes: 
superseded-by:
---

# ADR-0001: Build the control plane as a modular monolith in a monorepo

## Context and problem statement

The control plane covers ~20 domains (tenancy, targets, discovery, runs, findings,
diagnosis, repair, verification, audit, quotas…) and must reach 99.9% availability (NFR-3)
with a small team (3–6 engineers, Assumed). The brief names FastAPI for the control plane,
Celery for jobs and LangGraph for agents, and Next.js for the dashboard. We must decide how
to split this into deployables and repositories.

## Decision drivers

- Small team, fast iteration, low operational overhead.
- Clear domain boundaries so hot domains (runs, agents) can be extracted later.
- Shared contracts between web, API, runner and agents (OpenAPI client, Workflow IR schema).
- Independent scaling of API vs background work.

## Considered options

1. **Modular monolith**: one Python package, several process types (api, worker-<queue>, scheduler, relay), enforced module boundaries; monorepo with web, runner and infra.
2. Microservices per domain (discovery, execution, diagnosis, repair services).
3. Plain monolith without enforced boundaries.

## Decision outcome

Chosen option: "**Modular monolith in a monorepo**", because it gives independent scaling
per process type (API tasks vs agent workers vs default workers) without distributed
transactions, network hops between domains, or per-service operations, while enforced
module boundaries keep extraction possible. The execution plane is a separate deployable
anyway (ADR-0008) because its trust level differs, not because of team structure.

### Confidence

Recommended.

### Consequences

- Good: one deploy pipeline for the control plane; transactions across modules where needed (e.g. run + outbox + audit).
- Good: shared types; atomic changes across web/API/IR schema in one PR.
- Bad / trade-off: one bad dependency upgrade affects all control-plane processes; needs import-linter rules.
- Follow-up: `import-linter` contracts in CI (modules only import other modules' `service` interfaces).

### Confirmation

CI import-linter contracts; no module imports another module's `repository` or `models`;
architecture review at each phase gate.

## Pros and cons of the options

### Modular monolith
- Good: simplest operations; fast; extraction path preserved.
- Bad: requires discipline on boundaries.

### Microservices
- Good: independent deploys and scaling per domain.
- Bad: distributed data and failure modes, more infra, premature for team size and load (< 100 req/s API).

### Plain monolith
- Good: least ceremony.
- Bad: boundaries erode; agent code tangles with safety-critical admission/verification code.

## More information

Revisit if: a domain needs a different runtime or scaling profile that process types can't
give (e.g. agent workers need GPUs), or more than ~3 teams work on the control plane.
Layout: design.md §8, low-level-design.md §1.
