---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [integration, security, repair]
supersedes: 
superseded-by:
---

# ADR-0017: Integrate with GitHub via a GitHub App that only opens pull requests

## Context and problem statement

We need repository read access (discovery, sandbox builds, repair base) and the ability to
open PRs with verified patches (FR-27), with human approval before anything is proposed and
never an automatic merge or deployment (NFR-7). Credentials with repo write access are the
most valuable secret we hold (threat model A11).

## Decision drivers

- Least privilege, per-repository scoping chosen by the customer.
- Short-lived credentials, never exposed to untrusted execution.
- Clear audit trail; impossible to merge or deploy by design.

## Considered options

1. **GitHub App** with permissions `contents: write`, `pull_requests: write`, `metadata: read` (+ `checks: read`, `deployments: read` optional) on customer-selected repos; installation tokens minted per job (1 h), down-scoped to one repository; branch prefix `reliability/`; PR creation by a deterministic job after human approval; no merge API calls exist in the codebase.
2. OAuth App / user tokens.
3. Personal access tokens provided by users.

## Decision outcome

Chosen option: "**GitHub App, PR-only**", because Apps provide per-repo installation,
short-lived tokens, bot identity on PRs, and webhooks, without acting as a user. Repository
content reaches sandboxes as tarballs fetched by the control plane — tokens never enter the
execution plane. The App key lives in Secrets Manager, readable only by the `github` worker.

### Confidence

Recommended.

### Consequences

- Good: minimal, auditable, revocable by the customer at any time.
- Bad / trade-off: `contents: write` is repo-wide (GitHub has no branch-scoped permission); mitigated by our branch prefix, audit, and recommending branch protection on default branches (accepted residual risk).
- Follow-up: GitLab/Bitbucket Deferred.

### Confirmation

Test asserting no code path calls merge/update-ref on default branch; PR creation only
reachable from `approvals` decision handler; audit event per PR.

## Pros and cons of the options

### GitHub App
- Good: least privilege, short-lived tokens, webhooks.
- Bad: App setup and key management.

### OAuth / user tokens
- Good: simple for users.
- Bad: acts as the user with broad scopes; long-lived tokens.

### PATs
- Good: trivial.
- Bad: long-lived, over-scoped, user-managed secrets.

## More information

threat-model.md A10–A11, A14; agent-workflows.md §6 (PR content).
