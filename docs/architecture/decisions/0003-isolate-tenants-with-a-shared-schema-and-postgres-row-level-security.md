---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [tenancy, security]
supersedes: 
superseded-by:
---

# ADR-0003: Isolate tenants with a shared schema and Postgres row-level security

## Context and problem statement

The platform is multi-tenant (brief, feature 8) and holds sensitive data per tenant: source
snapshots, findings that reveal weaknesses, telemetry, credentials. NFR-6 demands zero
cross-tenant access. Expected tenants: hundreds of small orgs (NFR-10). We must choose how
data is partitioned and how isolation is enforced across Postgres, object storage,
telemetry and execution.

## Decision drivers

- Isolation that fails closed when a developer forgets a filter.
- Many small tenants → per-tenant databases/schemas are operationally expensive.
- Consistency across all stores, not only Postgres.

## Considered options

1. **Shared schema + `org_id` on every tenant table + RLS (FORCE) + tenant context per transaction**.
2. Shared schema with application-level scoping only (repository filters).
3. Schema per tenant.
4. Database per tenant.

## Decision outcome

Chosen option: "**Shared schema with RLS**", because it gives database-enforced isolation
that fails closed (`current_setting('app.org_id')` errors when unset) at the cost profile
of a shared schema. The same tenant key is applied everywhere else: object keys prefixed
`org/<org_id>/` and only reachable through presigned URLs minted after authorization; LGTM
tenant header `X-Scope-OrgID = org_id` set by the OTLP/query gateway; per-run Docker
networks in the execution plane; Redis keys namespaced.

### Confidence

Recommended.

### Consequences

- Good: defense in depth; cheap to operate; cross-tenant analytics for operators via an audited role.
- Bad / trade-off: every table and migration must include `org_id` and a policy; slight query overhead.
- Follow-up: migration helper that generates policies; contract test suite proving 404 on foreign ids for every resource.

### Confirmation

CI check that every table with `org_id` has RLS enabled and forced; two-tenant contract
suite; production alarm on RLS errors.

## Pros and cons of the options

### Shared schema + RLS
- Good: fails closed; low cost.
- Bad: noisy-neighbor risk on shared DB (mitigated by quotas).

### App-level scoping only
- Good: simplest.
- Bad: one missed filter = data breach.

### Schema / database per tenant
- Good: strongest isolation, per-tenant restore.
- Bad: migrations × N, connection sprawl; unjustified for small tenants.

## More information

Revisit for an enterprise tier with contractual isolation or data residency → database per
tenant for those tenants only. data-model.md §4.
