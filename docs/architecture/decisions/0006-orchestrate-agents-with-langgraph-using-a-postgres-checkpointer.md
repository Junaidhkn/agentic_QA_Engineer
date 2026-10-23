---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [agents, orchestration]
supersedes: 
superseded-by:
---

# ADR-0006: Orchestrate agents with LangGraph using a Postgres checkpointer

## Context and problem statement

Discovery, test generation, diagnosis and repair are multi-step, partly agentic workflows
lasting minutes to hours, with human review points (approve workflows, approve patches) and
loops (fix → check → fix). They must survive worker restarts (Celery redelivery, deploys)
and be auditable. The brief names LangGraph.

## Decision drivers

- Explicit graphs with typed state, conditional edges and loops with bounds.
- Durable state and resume after crashes; human-in-the-loop pauses of days.
- Model-provider independence.
- Observability of each node.

## Considered options

1. **LangGraph graphs executed inside Celery tasks, PostgresSaver checkpointer, `interrupt()` for human steps, resume via API → new Celery task**.
2. LangGraph Platform / hosted server.
3. Hand-written state machines with direct LLM calls.

## Decision outcome

Chosen option: "**LangGraph in Celery workers with a Postgres checkpointer**", because it
gives durable, inspectable graphs in our own infrastructure and database (tenant-scoped via
`agent_threads`), with no additional server to run. Interrupts persist pending human
decisions; the API resumes threads with `Command(resume=…)`.

### Confidence

Confirmed (LangGraph from the brief); runtime placement Recommended.

### Consequences

- Good: crash-safe agent runs; one database to back up; clear node-level telemetry.
- Bad / trade-off: checkpoint tables grow — retention job prunes threads 30 days after completion; LangGraph version upgrades need checkpoint compatibility checks.
- Follow-up: node side effects must be idempotent (keys from `thread_id`, node, attempt).

### Confirmation

Chaos test: kill an agent worker mid-graph → thread resumes from last checkpoint without
duplicate side effects. Pin LangGraph version; upgrade tested against stored checkpoints.

## Pros and cons of the options

### LangGraph + Postgres in our workers
- Good: durable, self-hosted, matches stack.
- Bad: we operate scaling of agent workers ourselves.

### Hosted LangGraph server
- Good: built-in queues, streaming, UI.
- Bad: another platform and data location for customer code; cost; less control over tenancy.

### Hand-written state machines
- Good: no framework dependency.
- Bad: reinvents checkpointing, interrupts, streaming.

## More information

agent-workflows.md §2.
