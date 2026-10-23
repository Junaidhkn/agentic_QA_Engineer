---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [realtime, frontend]
supersedes: 
superseded-by:
---

# ADR-0021: Stream live run progress over Server-Sent Events

## Context and problem statement

During a run, users watch VUs, RPS, percentiles, errors and generator health with ≤ 5 s
delay (FR-15), and see step progress for long agent jobs. Data flows one way (server →
browser). The API runs as multiple stateless instances.

## Decision drivers

- One-way push, simple reconnection, works through proxies/CDN.
- Horizontal scaling of API instances.
- Graceful degradation.

## Considered options

1. **SSE from FastAPI (`/v1/runs/{id}/events`) with `Last-Event-ID` resume; runners post events → API persists to `run_events` and publishes to Redis pub/sub; each SSE connection subscribes to the run channel. Fallback: polling.**
2. WebSockets.
3. Polling only.
4. Managed real-time service.

## Decision outcome

Chosen option: "**SSE with Redis pub/sub backplane**", because the traffic is one-way, SSE
reconnects natively with resume, and Redis pub/sub lets any API instance serve any run.
Progress events are aggregated by the runner every 2 s, so fan-out volume is small.

### Confidence

Recommended.

### Consequences

- Good: simple, scalable, resumable.
- Bad / trade-off: long-lived connections need ALB idle timeout > heartbeat (15 s heartbeats); Redis outage pauses live view (polling fallback from Postgres).

### Confirmation

Load test 500 concurrent SSE clients; reconnect test with `Last-Event-ID`.

## Pros and cons of the options

### SSE
- Good: simplest for one-way.
- Bad: one-way only.

### WebSockets
- Good: bidirectional.
- Bad: unneeded complexity.

### Polling
- Good: trivial.
- Bad: latency vs load trade-off.

### Managed service
- Good: no connection infra.
- Bad: another processor of tenant data; cost.

## More information

api-contract.md §6.
