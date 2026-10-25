---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [execution, security, deployment]
supersedes: 
superseded-by:
---

# ADR-0008: Run all untrusted execution in a separate execution plane with pull-based runners

## Context and problem statement

The platform executes untrusted code: customer repository builds (install scripts),
generated Playwright/k6 scripts, the app-under-test, coding-agent commands and security
scanners. It also emits load traffic to the internet. A compromise of this code must not
reach the control plane's database, Redis, secrets, GitHub App key, or other tenants
(threat model A4–A6). NFR-4 also needs fast job pickup.

## Decision drivers

- Blast-radius containment for untrusted code.
- No control-plane credentials anywhere untrusted code runs.
- Fast, reliable job handoff (NFR-4) and kill within 10 s (NFR-7).
- Independent scaling of execution capacity.

## Considered options

1. **Separate cloud account/VPC; runner agents on hosts pull leases from an mTLS lease API over PrivateLink; results via presigned URLs and run-scoped tokens; no DB/Redis access.**
2. Execution as Celery workers in the control-plane VPC with Docker-in-worker.
3. Push model: control plane calls into execution hosts (inbound API on hosts).

## Decision outcome

Chosen option: "**Separate execution plane with pull-based runners**", because the pull
model needs no inbound connectivity to execution hosts, leases bound each job's
permissions (one subject, short tokens, presigned URLs, sealed secrets), heartbeats give a
natural cancel channel for the kill switch, and a separate account makes IAM mistakes in
one plane unable to grant access to the other. This mirrors proven CI-runner designs.

### Confidence

Recommended.

### Consequences

- Good: compromise of a host yields at most the active leases' scoped tokens.
- Good: execution capacity scales independently (ASG on queue depth).
- Bad / trade-off: lease protocol to build and test; cross-account networking (PrivateLink) to operate.
- Follow-up: host identity (mTLS certs, 24 h) issuance at boot; lease reaper; kill latency tests.

### Confirmation

Nightly runner integration test: from inside a sandbox, control-plane endpoints, Redis,
RDS and metadata IPs are unreachable; kill lands in < 10 s; leases cannot be acquired
without a host cert.

## Pros and cons of the options

### Separate plane, pull runners
- Good: strongest containment, no inbound ports, clean cancel path.
- Bad: more infrastructure.

### Celery workers with Docker
- Good: reuses queue stack.
- Bad: workers hold broker/DB credentials next to untrusted code; Docker socket = root on host.

### Push to hosts
- Good: lower latency.
- Bad: inbound attack surface on hosts; host discovery and health management.

## More information

low-level-design.md §5; api-contract.md §7; operations.md §1.
