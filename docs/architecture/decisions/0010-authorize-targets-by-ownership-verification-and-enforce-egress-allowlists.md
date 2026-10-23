---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [safety, security, targets]
supersedes: 
superseded-by:
---

# ADR-0010: Authorize targets by ownership verification and enforce egress allowlists; no production load tests in MVP

## Context and problem statement

A load-testing service can be weaponized to attack third parties (threat model A1), and the
brief requires "zero unauthorized production load tests" and excludes "unrestricted testing
of third-party or unauthorized targets". We need a mechanism that proves the customer
controls a host, and an enforcement point that the rest of the system cannot bypass.

## Decision drivers

- Prevent traffic to hosts the customer doesn't control.
- Enforcement independent of application bugs (network layer).
- Low friction for indie developers (Vercel/Netlify/Render preview hosts are common).
- Clear production policy.

## Considered options

1. **Per-host proof of control (DNS TXT at `_reliability-engineer.<host>` or token at `/.well-known/reliability-engineer.txt`), re-verified at admission if older than 24 h and by hourly sweep; egress proxy enforcing per-run host + IP allowlists; environment classification with production restricted to approved smoke runs.**
2. Terms-of-service attestation only (checkbox).
3. Require customers to install an agent/sidecar in their app that authorizes traffic.

## Decision outcome

Chosen option: "**Ownership verification + egress allowlists + environment policy**",
because it is the industry-standard proof of control (as used for domain validation),
works on shared hosting subdomains (well-known file), and the egress proxy enforces it even
if admission has a bug. Additional rules:
- Each additional host a workflow calls (e.g. `api.` subdomain) must be verified separately.
- Hosts resolving to private, loopback, link-local or metadata ranges are rejected (SSRF).
- Cross-host redirects are not followed by generators.
- `production` targets: only smoke profile, ≤ 5 VUs, ≤ 60 s, owner approval, two-person rule for marking a host production-allowed. Load/stress/spike/soak on production are refused in MVP (Deferred).
- First load run against a target and any run > 500 VUs require owner/admin approval.
- Fixed, published egress IPs so customers can allowlist us in WAFs; abuse contact on IPs.

### Confidence

Recommended.

### Consequences

- Good: platform cannot be pointed at arbitrary sites; strong story for abuse reports.
- Bad / trade-off: onboarding step (DNS or file) adds friction; mislabeling production as staging can't be fully prevented (accepted residual risk with heuristics + caps).
- Follow-up: production-host heuristics (GitHub deployments API environment URLs) to warn users.

### Confirmation

Contract tests for admission; runner integration test proving egress to non-allowlisted
host fails; monitoring alert on any proxy denial.

## Pros and cons of the options

### Verification + allowlists
- Good: real proof, network-enforced.
- Bad: setup friction.

### ToS only
- Good: zero friction.
- Bad: no protection; legal and reputational exposure.

### Customer-side agent
- Good: strongest consent signal, enables telemetry.
- Bad: heavy onboarding for vibe-coded apps; still needs network enforcement.

## More information

threat-model.md A1–A3; low-level-design.md §4 (admission).
