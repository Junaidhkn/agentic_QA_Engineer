---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [execution, security, deployment, kubernetes]
supersedes: 
superseded-by:
---

# ADR-0009: Isolate untrusted execution in gVisor-sandboxed Docker on dedicated hosts; defer Kubernetes

## Context and problem statement

The brief asks for "isolated Docker workers" and "Kubernetes when justified". Standard
Docker (runc) shares the host kernel; it is not a sufficient boundary for hostile code
(threat model A4). We need a sandbox runtime and a host orchestration approach that a small
team can operate, sized for ~20 hosts at peak (operations.md §4).

## Decision drivers

- Strong isolation for untrusted code and network control per run.
- Container-level startup speed (NFR-4).
- Compatibility with Docker images and docker-compose apps (ADR-0011).
- Operational simplicity; Kubernetes only with a stated trigger.

## Considered options

1. **EC2 hosts in an ASG running Docker with gVisor (`runsc`) as default runtime, driven by our runner agent; per-run Docker networks behind an egress proxy.**
2. Firecracker microVMs (self-managed, or a managed microVM platform).
3. Kubernetes (EKS) with gVisor RuntimeClass, NetworkPolicy and k6-operator.
4. AWS Fargate tasks per job (Firecracker-backed).
5. Plain Docker (runc) with seccomp/AppArmor.

## Decision outcome

Chosen option: "**Docker + gVisor on dedicated EC2 hosts, Kubernetes Deferred**", because
gVisor gives a user-space kernel boundary with Docker compatibility and sub-3-s container
starts; the runner agent already handles scheduling within a host, and ASG + queue-depth
scaling covers placement. Hardening: non-root, no capabilities, seccomp, read-only rootfs
where possible, cgroup limits (cpu, memory, pids, disk), no host mounts, per-run networks,
metadata blocked, hosts recycled after 50 runs / 24 h.

### Confidence

Recommended (Docker workers from the brief; gVisor and K8s deferral are recommendations).

### Consequences

- Good: strong isolation without a cluster to run.
- Bad / trade-off: gVisor adds syscall overhead (I/O-heavy builds slower; load generator network path should be benchmarked); some workloads may need `runsc` platform tuning (systrap/KVM).
- Bad / trade-off: we build host-level scheduling (bin-packing by slots) ourselves.
- Follow-up: NFR-1 benchmark run **under gVisor** for the generator; if overhead invalidates results, generators may run under runc on dedicated generator-only hosts (they execute our own generated scripts, not customer code) — decision recorded then.

### Confirmation

Runner integration tests (escape attempts, resource exhaustion); benchmark of k6 under
gVisor vs runc in Phase 3.

**Kubernetes triggers (Deferred):** > ~20 execution hosts sustained; distributed load via
k6-operator (> 1 generator per run, > 1,000 VUs); multi-region generators; or a dedicated
platform team.

## Pros and cons of the options

### Docker + gVisor on EC2
- Good: strong boundary, Docker-compatible, simple ops.
- Bad: syscall overhead; custom scheduling.

### Firecracker microVMs
- Good: VM-level isolation, fast boot.
- Bad: needs bare-metal/nested virt and more tooling; compose apps harder.

### EKS + gVisor
- Good: mature scheduling, k6-operator, NetworkPolicy.
- Bad: cluster operations unjustified at MVP scale.

### Fargate per job
- Good: VM isolation, no hosts.
- Bad: slower starts (often 30–60 s+, threatens NFR-4), limited network control per task, no compose.

### Plain Docker
- Good: simplest.
- Bad: shared kernel with hostile code.

## More information

operations.md §1, §4; threat-model.md A4–A6.
