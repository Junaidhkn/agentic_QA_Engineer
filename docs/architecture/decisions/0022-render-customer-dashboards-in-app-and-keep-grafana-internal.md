---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [frontend, observability, tenancy]
supersedes: 
superseded-by:
---

# ADR-0022: Render customer dashboards in the Next.js app; keep Grafana internal

## Context and problem statement

The brief names a Next.js + TypeScript dashboard and Grafana for observability. Customers
need run charts, trends, comparisons and evidence views tightly integrated with findings,
hypotheses and repairs. Embedding Grafana per tenant requires per-tenant Grafana orgs or
data-source proxies, separate auth, and careful iframe security.

## Decision drivers

- Integrated UX (evidence links, comparisons with CIs, approvals).
- Tenant isolation and a single auth system.
- Accessibility (WCAG 2.1 AA) and consistent design.

## Considered options

1. **Customer-facing charts rendered by the Next.js app (ECharts) from API endpoints that query LGTM with the caller's tenant; Grafana used internally by operators and support (SSO).**
2. Embed Grafana dashboards per tenant.
3. Both: in-app plus "open in Grafana" for power users.

## Decision outcome

Chosen option: "**In-app charts; Grafana internal**", because the API stays the only path to
tenant data (authz in one place), the UI can link evidence to hypotheses and repairs, and we
avoid running multi-tenant Grafana. Option 3 is Deferred (trigger: power-user demand for
ad-hoc PromQL; would use per-tenant Grafana orgs with read-only data sources).

### Confidence

Recommended (Next.js from the brief; Grafana placement recommended).

### Consequences

- Good: cohesive product, single authz path.
- Bad / trade-off: we build and maintain chart views; ad-hoc querying not available to customers.

### Confirmation

No customer route proxies Grafana; chart endpoints covered by tenancy contract tests.

## Pros and cons of the options

### In-app
- Good: integration, security, accessibility.
- Bad: chart development effort.

### Embedded Grafana
- Good: rich, free dashboards.
- Bad: multi-tenant Grafana ops, auth bridging, iframe risks.

### Both
- Good: flexibility.
- Bad: doubles surface area now.

## More information

low-level-design.md §11; ADR-0014.
