---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [auth, security, assumed]
supersedes: 
superseded-by:
---

# ADR-0018: Authenticate with a managed OIDC provider and enforce RBAC in the API

## Context and problem statement

The brief places authentication in the FastAPI control plane. Users are developers who
mostly have GitHub accounts; teams need roles; CI needs machine credentials (FR-32); owners
and admins need MFA (threat model A13). Building password storage, MFA and recovery
ourselves is risky and unnecessary.

## Decision drivers

- Security of identity flows without building them.
- Low-friction sign-in for developers (GitHub login).
- Authorization owned by our API (roles, tenancy).

## Considered options

1. **Managed OIDC IdP (category: Auth0 / Clerk / WorkOS / Cognito) for sign-in (GitHub social + email magic link, MFA); FastAPI validates IdP JWTs and owns orgs, memberships, roles and API keys.**
2. Self-hosted IdP (Keycloak / Ory).
3. Hand-built auth in FastAPI (passwords, sessions).

## Decision outcome

Chosen option: "**Managed OIDC IdP + RBAC in FastAPI**", because it removes the riskiest
identity work while keeping authorization — the part that depends on our domain — in one
policy layer (`tenancy.require`). The API is bearer-token only (no cookies → no CSRF);
API keys for CI are created and verified by our API (hashed with pepper, scoped,
expiring). The specific vendor is a Phase 0 choice behind an `IdentityVerifier` adapter.

### Confidence

Assumed (the brief says "FastAPI for authentication"; this keeps verification and
authorization in FastAPI but delegates credential handling).

### Consequences

- Good: MFA, social login, breach-resistant flows out of the box.
- Bad / trade-off: per-MAU vendor cost; vendor outage blocks sign-in (existing tokens keep working until expiry).
- Follow-up: SSO/SAML Deferred (trigger: team-plan customer requirement).

### Confirmation

Contract tests for role matrix; JWT validation tests (iss, aud, exp, alg pinned); MFA
enforcement check for owner/admin at login (IdP rule) and at sensitive endpoints (claim check).

## Pros and cons of the options

### Managed IdP
- Good: secure, fast to ship.
- Bad: cost, vendor dependency.

### Self-hosted IdP
- Good: control, no per-user fees.
- Bad: another critical system to run and patch.

### Hand-built
- Good: no dependency.
- Bad: high risk, slow.

## More information

threat-model.md A13; api-contract.md §1.
