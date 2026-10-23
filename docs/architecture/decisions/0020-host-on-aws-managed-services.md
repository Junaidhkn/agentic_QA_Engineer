---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [deployment, cloud, assumed]
supersedes: 
superseded-by:
---

# ADR-0020: Host on AWS managed services

## Context and problem statement

The design needs: managed Multi-AZ Postgres and Redis (NFR-3, NFR-9), S3-compatible storage,
KMS and a secret manager, container hosting for stateless services, VM hosts with custom
runtimes (gVisor) for the execution plane, cross-account private connectivity
(PrivateLink), fixed egress IPs, and a WAF/CDN. The brief does not name a cloud.

## Decision drivers

- Managed equivalents for every data component.
- Strong account-level isolation for the execution plane (ADR-0008).
- Team familiarity and hiring market (Assumed).

## Considered options

1. **AWS: ECS Fargate (control plane), EC2 ASG (execution plane), RDS PostgreSQL, ElastiCache Redis, S3, KMS, Secrets Manager, PrivateLink, NAT with Elastic IPs, CloudFront + WAF, Route 53; Terraform.**
2. GCP equivalents (Cloud Run, GCE, Cloud SQL, Memorystore, GCS).
3. PaaS (Render/Fly/Railway) for control plane + separate VM provider for execution.

## Decision outcome

Chosen option: "**AWS managed services**", because every component has a mature managed
equivalent, multi-account isolation is idiomatic, and S3 is the reference implementation of
the S3 API the brief asks for. All components are reached through adapters/standard
protocols (S3 API, Postgres, Redis, OTLP), so a move to GCP stays feasible.

### Confidence

Assumed — confirm cloud preference and any credits/commitments.

### Consequences

- Good: mature, well-documented, PrivateLink and account isolation.
- Bad / trade-off: NAT and egress costs; vendor-specific IaC.
- Follow-up: AWS Organizations with separate accounts: `control-prod`, `exec-prod`, `staging`, `shared-logging`.

### Confirmation

Terraform modules per plane; no AWS SDK use outside adapters (`ObjectStore`, `KmsClient`).

## Pros and cons of the options

### AWS
- Good: complete managed portfolio.
- Bad: cost complexity.

### GCP
- Good: simpler networking, Cloud Run.
- Bad: no stronger fit for our needs; team familiarity unknown.

### PaaS + VMs
- Good: fastest start for control plane.
- Bad: weaker private connectivity to execution plane, fewer compliance controls.

## More information

operations.md §1.
