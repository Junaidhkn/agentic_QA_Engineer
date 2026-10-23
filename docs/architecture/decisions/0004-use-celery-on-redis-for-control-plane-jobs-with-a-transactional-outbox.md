---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [async, reliability]
supersedes: 
superseded-by:
---

# ADR-0004: Use Celery on Redis for control-plane jobs, with a transactional outbox

## Context and problem statement

Most platform work is asynchronous: discovery, test generation, diagnosis and repair graphs
(minutes to hours), run analysis, GitHub calls, report generation, retention. The brief names
Redis/Celery. NFR-4 requires 95% of queued jobs to start within 30 s. Celery on Redis has
known hazards: tasks lost on worker crash without late acks, visibility-timeout redelivery
of long tasks, and enqueue/commit races (job published but DB transaction rolled back, or
vice versa).

## Decision drivers

- Brief names Celery + Redis.
- Exactly-once *effects* for state transitions (runs, repairs) despite at-least-once delivery.
- Long agent workflows must not depend on a single long-lived task.
- Run dispatch must not queue behind agent work.

## Considered options

1. **Celery + Redis broker, Postgres as state of record, transactional outbox, short idempotent tasks, per-workload queues**.
2. Celery + Redis used directly from request handlers (`.delay()` inside transactions).
3. Postgres-backed queue only (e.g. SKIP LOCKED worker).
4. Workflow engine (Temporal).

## Decision outcome

Chosen option: "**Celery on Redis with a transactional outbox**", because it keeps the
named stack while removing its main failure modes:
- Services write state + an `outbox` row in one transaction; a relay publishes to Celery.
- Postgres state machines (guarded transitions) are the truth; Celery messages carry ids only.
- `acks_late=True`, `prefetch_multiplier=1`, visibility timeout > longest task; long agent
  graphs are resumable from LangGraph checkpoints (ADR-0006), so a redelivered task resumes
  rather than restarts.
- Separate queues/worker pools: `default` (dispatch, analysis), `agents`, `github`.
- Reapers detect stuck subjects and re-enqueue.
- Execution-plane jobs do **not** go through Celery; they are leased to runners (ADR-0008).

### Confidence

Confirmed (Celery + Redis from the brief); outbox and queue layout Recommended.

### Consequences

- Good: no lost or phantom jobs; queue isolation protects NFR-4.
- Bad / trade-off: relay process and outbox table to operate; ~1 s extra dispatch latency (mitigated by NOTIFY).
- Follow-up: outbox lag metric and alarm.

### Confirmation

Integration tests: kill worker mid-task → subject completes once; rollback after enqueue →
no job runs. Queue age SLO dashboard.

## Pros and cons of the options

### Celery + outbox
- Good: named stack, robust semantics.
- Bad: more moving parts than plain Celery.

### Plain Celery `.delay()`
- Good: simplest.
- Bad: dual-write races, lost tasks.

### Postgres queue only
- Good: one store, transactional enqueue.
- Bad: diverges from brief; polling load on primary at higher volumes.

### Temporal
- Good: durable workflows, retries, human steps natively.
- Bad: new heavy infrastructure; LangGraph already provides durable agent state.

## More information

Revisit (Temporal or similar) if non-agent multi-step workflows with compensation multiply
beyond verification. low-level-design.md §3, §10.
