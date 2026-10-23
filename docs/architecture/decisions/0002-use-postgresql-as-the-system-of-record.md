---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [data-store]
supersedes: 
superseded-by:
---

# ADR-0002: Use PostgreSQL as the system of record

## Context and problem statement

The platform stores strongly relational metadata (orgs → projects → targets, suites, runs,
findings → evidence → hypotheses → repairs → gates), needs transactions across them (run +
outbox + audit; quota reservation), integrity constraints, tenant isolation (NFR-6), and
durable agent state for resumable LangGraph threads (ADR-0006). The brief names PostgreSQL.

## Decision drivers

- Transactions and constraints (state machines, uniqueness of fingerprints and leases).
- Row-level security for multi-tenancy.
- JSONB for semi-structured data (Workflow IR, summaries, evidence locators).
- A supported LangGraph checkpointer.
- Managed, Multi-AZ, PITR (NFR-3, NFR-9).

## Considered options

1. **PostgreSQL 16 (managed, RDS Multi-AZ)**.
2. Document database (MongoDB) for runs/findings + relational for tenancy.
3. Postgres + a time-series DB for run metrics in the same store (TimescaleDB).

## Decision outcome

Chosen option: "**PostgreSQL 16 on RDS Multi-AZ**", because it meets every driver in one
store; high-volume time series go to the Prometheus-compatible store (ADR-0014) and only
per-run summaries live in Postgres, so no time-series extension is needed.

### Confidence

Confirmed (named in the brief).

### Consequences

- Good: one source of truth for state, agent checkpoints, outbox and audit.
- Bad / trade-off: append-heavy tables need monthly partitioning and retention jobs.
- Follow-up: pg_partman, RDS Proxy if connection counts grow, read replica for reports when needed.

### Confirmation

Schema in data-model.md implemented via Alembic; integration tests run against real
Postgres with RLS on.

## Pros and cons of the options

### PostgreSQL
- Good: relational integrity, RLS, JSONB, mature managed offerings.
- Bad: write scaling is vertical; fine at our volume (< 200k rows/day).

### Document DB + relational
- Good: flexible documents for run results.
- Bad: two stores, cross-store consistency, weaker tenant isolation tooling.

### TimescaleDB for metrics
- Good: SQL over time series.
- Bad: duplicates the Prometheus/Grafana ecosystem the brief requires; load on the primary.

## More information

data-model.md; revisit if write volume exceeds a single primary (> ~5k writes/s sustained).
