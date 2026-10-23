---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [execution, verification, assumed]
supersedes: 
superseded-by:
---

# ADR-0011: Verify patches in ephemeral sandbox builds, with a customer preview-URL fallback

## Context and problem statement

To "re-run functional, security, and load tests against patched builds" and "compare
baseline versus patched performance", we need somewhere to run the baseline and patched
versions. Customer staging environments are shared, noisy and deploy only what the customer
deploys; we cannot deploy patches to them (no automatic deployments). Many vibe-coded apps
depend on hosted backends (Supabase, Firebase, third-party APIs) that are hard to reproduce.

## Decision drivers

- Apples-to-apples comparison (same resources, data, workload) for credible verdicts.
- Server-side telemetry for diagnosis (auto-instrumentation, DB stats) — unavailable on most customer staging apps.
- No deployment to customer infrastructure.
- Coverage of common stacks.

## Considered options

1. **Build and run the app in an ephemeral sandbox environment (compose → Dockerfile → buildpacks), with ephemeral Postgres/Redis, migrations + seed, stubbed external HTTP, OTel auto-instrumentation; if the app can't be reproduced, fall back to a customer-provided, verified preview URL per commit (e.g. Vercel/Netlify/Render preview deployments of the PR branch).**
2. Customer preview URLs only.
3. Sandbox builds only (reject apps that can't be built).

## Decision outcome

Chosen option: "**Sandbox builds first, preview-URL fallback**", because sandbox builds give
controlled, instrumented, comparable environments for the cases we can reproduce, while the
fallback keeps the product useful for BaaS-heavy apps. Fallback comparisons are labelled
"environment not controlled", need ≥ 5 runs per side, and cap hypothesis confidence at
`medium` when server telemetry is absent.

Initial supported build paths: docker-compose, Dockerfile, buildpacks for Node (Next.js,
Express) and Python (FastAPI); ORMs for migrations: Prisma, Drizzle, SQLAlchemy/Alembic.

### Confidence

Assumed — depends on the real distribution of customer stacks; validated by onboarding
success metrics in private beta.

### Consequences

- Good: high-quality evidence and comparisons where it works.
- Bad / trade-off: significant engineering (stack detection, seeding, stubs); some apps will fail to build (top product risk).
- Follow-up: per-stack build success dashboard; Deferred: Supabase/Firebase local emulation (trigger: > 30% onboarding blocked by BaaS).

### Confirmation

Onboarding success by stack (evaluation.md §7); verification gate records environment type.

## Pros and cons of the options

### Sandbox first + fallback
- Good: best evidence, broad applicability.
- Bad: two paths to maintain.

### Preview URLs only
- Good: little build engineering.
- Bad: noisy comparisons, no server telemetry, depends on customer CI.

### Sandbox only
- Good: one controlled path.
- Bad: excludes many target customers.

## More information

low-level-design.md §5; evaluation.md §6.
