---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [observability, tenancy]
supersedes: 
superseded-by:
---

# ADR-0014: Collect evidence telemetry with OpenTelemetry into a multi-tenant Grafana LGTM stack

## Context and problem statement

The brief names OpenTelemetry, Prometheus and Grafana for logs, traces, latency,
throughput, errors and resource use. Two different telemetry needs exist: (a) **evidence
telemetry** about customer apps under test (k6 metrics, app traces/logs, DB and container
metrics) — tenant data used for findings, diagnosis and charts; (b) **platform telemetry**
about our own services. Evidence telemetry must be tenant-isolated (NFR-6), queryable by
the diagnosis tools, and cheap to retain for 30 days.

## Decision drivers

- Prometheus-compatible querying (PromQL), traces and logs, correlated by trace id.
- Native multi-tenancy with a header we control.
- S3-backed storage for cost; single-binary operation at MVP scale.

## Considered options

1. **OTel everywhere → OTLP gateway (OTel Collector: auth, tenant tagging, tail sampling) → Mimir (metrics, Prometheus-compatible), Tempo (traces), Loki (logs), each multi-tenant via `X-Scope-OrgID`, S3 backend, single-binary mode; Grafana for internal use.**
2. One Prometheus server per run inside the sandbox; snapshot TSDB to S3 at the end.
3. Hosted observability vendor with per-tenant projects.
4. Store all run telemetry in Postgres.

## Decision outcome

Chosen option: "**OTel → gateway → multi-tenant LGTM on S3**", because it satisfies the
named stack (Prometheus-compatible metrics, Grafana), provides tenancy natively with a header
only our gateway sets, and keeps cost low with object storage. k6 exports metrics via
Prometheus remote write / OTLP tagged with run, scenario, endpoint (path template only, to
bound cardinality). Per-run headline summaries are copied into Postgres
(`run_metric_summaries`) and evidence snapshots into `evidence.snapshot`, so findings remain
meaningful after the 30-day telemetry retention.

Platform telemetry uses the same pipeline under tenant `platform` (or Grafana Cloud if we
prefer not to self-host it — operational choice, not architectural).

### Confidence

Recommended.

### Consequences

- Good: one query language set for charts, diagnosis tools and ops; tenant-safe.
- Bad / trade-off: three stateful components to operate; capacity planning for series/spans (operations.md §4).
- Follow-up: cardinality limits per tenant; tail-sampling policy; gateway persistent queue (1 h) for backend outages.

### Confirmation

Tenancy test: query with tenant A token never returns tenant B series; gateway overwrites
client-supplied tenant attributes (unit + integration tests).

## Pros and cons of the options

### LGTM multi-tenant
- Good: named ecosystem, tenancy, S3 cost.
- Bad: ops burden.

### Per-run Prometheus
- Good: perfect isolation, simple.
- Bad: no cross-run trends; traces/logs separate; re-hydration for diagnosis is awkward.

### Hosted vendor
- Good: no ops.
- Bad: per-tenant cost, data-location concerns for customer telemetry.

### Postgres
- Good: one store.
- Bad: wrong tool for high-volume time series and traces.

## More information

api-contract.md §8; operations.md §6; ADR-0022 (customer charts in-app).
