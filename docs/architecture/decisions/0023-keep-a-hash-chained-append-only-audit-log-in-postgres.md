---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [security, compliance, audit]
supersedes: 
superseded-by:
---

# ADR-0023: Keep a hash-chained, append-only audit log in Postgres

## Context and problem statement

The brief requires audit logs; FR-29 lists security-relevant events including agent tool
calls and approvals. Customers (and SOC 2 readiness, NFR-14) need tamper-evident records of
who started load against which host, who approved which PR, and what agents did.

## Decision drivers

- Tamper evidence, including against insiders (threat model A15).
- Per-tenant querying and export.
- Low operational cost.

## Considered options

1. **`audit_events` table in Postgres, INSERT-only grants, per-org SHA-256 hash chain, monthly partitions; daily anchor (latest hash per org) written to an S3 Object Lock (compliance mode) bucket; NDJSON export with chain for verification.**
2. Ship audit events to a log system (Loki/CloudWatch) only.
3. Managed ledger database.

## Decision outcome

Chosen option: "**Hash-chained Postgres table with WORM anchors**", because it keeps audit
writes in the same transaction as the action (no lost events), supports tenant queries via
RLS, and makes tampering detectable (any modification breaks the chain relative to the
WORM anchor).

### Confidence

Recommended.

### Consequences

- Good: transactional, queryable, tamper-evident.
- Bad / trade-off: per-org serialization on writes (advisory lock) — fine at audit volumes (~50k/day); a DB superuser could still rewrite history, but not undetectably after an anchor.

### Confirmation

Daily job verifies chains against anchors and alerts on mismatch; tests ensure every
FR-29 action emits an event.

## Pros and cons of the options

### Postgres + anchors
- Good: transactional, cheap.
- Bad: write serialization per org.

### Logs only
- Good: simple.
- Bad: events can be lost; weak tenant querying; tampering harder to prove absent.

### Ledger DB
- Good: built-in verification.
- Bad: extra service and cost.

## More information

data-model.md §3 (`audit_events`); threat-model.md A15.
