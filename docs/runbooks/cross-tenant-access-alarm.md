# Cross-tenant access alarm

**Pages:** `RlsDenial` (a query was refused by row-level security) and `IntegrityViolation`
(the hourly invariant check found rows breaking an isolation or safety rule). Both should be
impossible. Treat as a security incident until proven otherwise.

## What the invariants mean (`modules/ops/integrity.py`)

| Invariant | Meaning |
|---|---|
| `cross_tenant_<table>` | A row's `org_id` differs from its project's org — data visible to the wrong tenant |
| `hypothesis_without_verified_support` | A root-cause hypothesis is stored without a verified citation (ADR-0015) |
| `verified_repair_without_passed_gates` | A repair is verified/approved/PR'd with a gate not passed (ADR-0016) |
| `pull_request_without_approval` | A PR exists without an approved approval (ADR-0017) |
| `audit_chain_broken` | An audit chain does not verify or contradicts its daily anchor (ADR-0023) |

## Steps

1. **Scope.** `SELECT detail FROM platform_checks WHERE name = 'integrity'` lists counts and
   broken chains. For RLS denials, search the API/worker logs for "row-level security" around
   the alert time: the request id gives route, principal and org.
2. **Contain.** If tenant data may have crossed orgs: disable the code path (feature flag via
   `reliability flag <key> on`, or roll back the last deploy), and suspend API keys or
   orgs involved. For a broken audit chain, snapshot the database now (`aws rds
   create-db-snapshot`) before anything else writes.
3. **Identify rows.** Each invariant's SQL is in `integrity.py`; run it with `SELECT *`
   instead of `count(*)` from a read-only session.
4. **Assess exposure.** Which orgs could read which rows, since when (`created_at`), and
   whether anyone did (audit events, API logs). Do not modify rows before the assessment is
   written down.
5. **Fix** the code, then the data (a reviewed SQL script, run with the owner role and
   recorded in the incident). Re-run `reliability integrity-check` — exit code 0 when clean.
6. **Notify** affected customers per the DPA (72 h for personal data breaches) after legal
   review.

## Verify

`reliability integrity-check` prints every invariant's count and exits non-zero on any
violation. The hourly job records it; the alert clears on the next clean run.
