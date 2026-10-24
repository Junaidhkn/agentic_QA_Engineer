# API Contract

> REST + OpenAPI 3.1, versioned under `/v1`. The FastAPI app generates the OpenAPI document;
> `packages/api-client` is generated from it. This file is the design contract that the
> generated spec must match.

## Contents
1. Conventions
2. Error model and codes
3. Rate-limit classes
4. Public API v1 — endpoint table
5. Key request/response schemas
6. Live run events (SSE)
7. Runner lease API (internal, execution plane)
8. Telemetry ingest (OTLP)
9. Webhooks

---

## 1. Conventions

| Topic | Rule |
|---|---|
| Base URL | `https://api.<domain>/v1` |
| Auth | `Authorization: Bearer <token>` — IdP access token (users) or API key `rek_<prefix>_<secret>` (CI). No cookies on the API → no CSRF surface. |
| Tenant | Derived from the resource (project → org) and checked against the caller's membership. `X-Org-Id` header selects org for org-level list endpoints. |
| Authorization | Role matrix per endpoint (column "Role"). Resources the caller cannot see return **404**, not 403. |
| Content type | `application/json; charset=utf-8`; unknown fields rejected (`422 VALIDATION_FAILED`). |
| IDs | Opaque strings (UUIDv7). |
| Time | RFC 3339 UTC. |
| Pagination | `?limit=50&cursor=<opaque>` → `{"data":[...], "next_cursor": "..."|null}`; max limit 200. |
| Idempotency | `Idempotency-Key` header (required where marked **I**); 24 h; same key + different body → `409 IDEMPOTENCY_CONFLICT`. |
| Async operations | `202 Accepted` + resource with `status`; poll `GET` or subscribe to events. |
| Request ID | Every response carries `X-Request-Id`; errors include it. |
| Versioning | Additive changes only within `/v1`; deprecations announced with `Deprecation` + `Sunset` headers. |

## 2. Error model and codes

```json
{
  "error": {
    "code": "TARGET_NOT_VERIFIED",
    "message": "Target staging.example.com has not been verified or its verification expired.",
    "details": {"target_id": "01J…", "verification_expires_at": "2026-10-20T10:00:00Z"},
    "request_id": "req_01J…"
  }
}
```

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed request |
| 401 | `UNAUTHENTICATED` | Missing/invalid/expired token |
| 403 | `FORBIDDEN` | Visible resource, insufficient role (e.g. viewer starting a run) |
| 403 | `ENVIRONMENT_POLICY_VIOLATION` | Profile not allowed on this environment (e.g. load on production) |
| 403 | `ORG_SUSPENDED` | Organization suspended (abuse/kill switch) |
| 404 | `NOT_FOUND` | Missing or not visible |
| 409 | `TARGET_NOT_VERIFIED` | Target pending/failed/expired/revoked |
| 409 | `INVALID_STATE` | Transition not allowed (e.g. approve an unverified repair) |
| 409 | `CONCURRENCY_LIMIT` | Org or target already at concurrent run limit |
| 409 | `IDEMPOTENCY_CONFLICT` | Key reused with a different body |
| 422 | `VALIDATION_FAILED` | Schema validation; `details.fields[]` |
| 422 | `VERIFICATION_FAILED` | Ownership proof not found; `details.observed` |
| 422 | `UNSAFE_TARGET` | Host resolves to private/link-local/metadata ranges or is on the platform denylist |
| 422 | `PLAN_LIMIT_EXCEEDED` | VUs/duration/profile type above plan |
| 429 | `QUOTA_EXCEEDED` | Monthly quota exhausted; `details.kind`, `details.resets_at` |
| 429 | `RATE_LIMITED` | Rate limit; `Retry-After` header |
| 202 | (not an error) `APPROVAL_REQUIRED` in body | Run created in `pending_approval` |
| 503 | `UNAVAILABLE` | Dependency down; `Retry-After` |

## 3. Rate-limit classes

Token buckets in Redis keyed by `(principal, class)`; on Redis failure: reads fail open,
run/agent creation fails closed.

| Class | Limit (per principal) | Endpoints |
|---|---|---|
| `read` | 600/min | GETs |
| `write` | 120/min | ordinary POST/PATCH/DELETE |
| `expensive` | 20/min, 200/day | discovery, suite generation, diagnosis, repair |
| `run` | 30/min | run creation |
| `verify` | 10/min per target | target verification |
| `auth` | 20/min per IP | API-key auth failures, invites |
| `ingest` | 20k spans/s per org (gateway) | OTLP |

## 4. Public API v1 — endpoint table

Role legend: V viewer · E engineer · A admin · O owner (higher roles include lower). **I** = Idempotency-Key required.

### Identity, orgs, keys

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| GET | /me | any | Current user + memberships | 200 | 401 |
| POST | /orgs | any | Create org (caller = owner) | 201 | 422 |
| GET | /orgs/{org_id} | V | Org, plan, usage summary | 200 | 404 |
| GET | /orgs/{org_id}/members | A | List members | 200 | |
| POST | /orgs/{org_id}/invitations | A | Invite by email with role | 201 | 422 |
| PATCH | /orgs/{org_id}/members/{user_id} | O | Change role | 200 | 409 last owner |
| DELETE | /orgs/{org_id}/members/{user_id} | A | Remove member | 204 | 409 last owner |
| POST | /orgs/{org_id}/api-keys | A | Create scoped key (secret shown once) | 201 | 422 |
| GET | /orgs/{org_id}/api-keys | A | List keys (prefix only) | 200 | |
| DELETE | /orgs/{org_id}/api-keys/{key_id} | A | Revoke | 204 | |
| GET | /orgs/{org_id}/usage | A | Quotas and consumption | 200 | |
| GET | /orgs/{org_id}/audit-events | A | Audit log (cursor; filters action, actor, from/to) | 200 | |
| GET | /orgs/{org_id}/audit-events:export | O | NDJSON export with hash chain | 200 | |

### Projects, GitHub, targets, secrets

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| POST | /projects | A | Create project | 201 | 422, 422 PLAN_LIMIT_EXCEEDED |
| GET | /projects | V | List | 200 | |
| GET / PATCH / DELETE | /projects/{id} | V / A / O | Read / update SLO defaults / archive | 200 / 200 / 204 | |
| POST | /integrations/github/install-url | A | URL to install the GitHub App (state bound to org) | 200 `{url}` | |
| GET | /integrations/github/callback | A | Setup redirect; records installation | 302 | 422 bad state |
| GET | /integrations/github/repositories | A | Repos visible to org installations | 200 | |
| PUT | /projects/{id}/repository | A | Link a repo `{installation_id, github_repo_id}` | 200 | 409 already linked |
| POST | /projects/{id}/targets | A | Register target; returns verification instructions | 201 | 422 UNSAFE_TARGET |
| GET | /projects/{id}/targets | V | List | 200 | |
| POST | /targets/{id}:verify | A | Check DNS TXT / well-known file | 200 | 422 VERIFICATION_FAILED |
| DELETE | /targets/{id} | A | Revoke (aborts in-flight runs) | 204 | |
| PUT | /projects/{id}/secrets/{name} | A | Write secret (write-only) | 204 | 422 |
| GET | /projects/{id}/secrets | E | Names + metadata only | 200 | |
| DELETE | /projects/{id}/secrets/{name} | A | Delete | 204 | |
| POST | /projects/{id}/openapi-specs | E | Register spec `{source:"upload"|"url"|"repo", ...}`; upload returns presigned PUT | 201 | 422 invalid spec |

### Discovery and workflows

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| POST | /projects/{id}/discoveries **I** | E | Start discovery `{commit_ref, openapi_spec_id?, browser_exploration?{target_id, action_budget, credential_refs}}` | 202 | 409 no repo, 409 TARGET_NOT_VERIFIED, 429 |
| GET | /discoveries/{id} | V | Status + stats + step progress | 200 | |
| POST | /discoveries/{id}:cancel | E | Cancel | 202 | 409 INVALID_STATE |
| GET | /projects/{id}/workflows | V | List (`status`, `kind`, `source` filters) | 200 | |
| POST | /projects/{id}/workflows | E | Add manual workflow (Workflow IR) | 201 | 422 IR invalid |
| GET / PATCH | /workflows/{id} | V / E | Read / edit IR, criticality, status | 200 | 422 |
| POST | /projects/{id}/workflows:bulk-review | E | `{approve:[ids], reject:[ids]}` | 200 | 409 destructive needs admin approval |
| GET | /projects/{id}/coverage | V | Discovered/approved/tested/passing counts per source & criticality | 200 | |

### Test suites and load profiles

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| POST | /projects/{id}/test-suites **I** | E | Generate suite from approved workflows `{workflow_ids?, commit_ref}` | 202 | 409 none approved, 429 |
| GET | /test-suites/{id} | V | Status + per-case validation | 200 | |
| GET | /test-suites/{id}/bundle | E | Presigned download of tests (zip) | 302 | |
| POST | /test-suites/{id}:export-pr **I** | E | Request PR adding tests to repo (approval path) | 202 | |
| GET/POST | /projects/{id}/load-profiles | V/E | List / create | 200/201 | 422 PLAN_LIMIT_EXCEEDED |
| GET/PATCH/DELETE | /load-profiles/{id} | V/E/E | Read / update / delete | 200/200/204 | |

### Runs

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| POST | /projects/{id}/runs **I** | E (or key scope `runs:write`) | Create run (admission control) | 202 run `queued` or `pending_approval` | 409 TARGET_NOT_VERIFIED, 403 ENVIRONMENT_POLICY_VIOLATION, 422 PLAN_LIMIT_EXCEEDED, 429 QUOTA_EXCEEDED, 409 CONCURRENCY_LIMIT |
| GET | /projects/{id}/runs | V | History (filters kind, status, profile, from/to) | 200 | |
| GET | /runs/{id} | V | Run + summary + verdict | 200 | |
| GET | /runs/{id}/events | V | SSE live stream (§6) | 200 text/event-stream | |
| GET | /runs/{id}/metrics | V | Time series `?series=p95_ms,rps,error_rate,vus&scope=…&step=5s` | 200 | 422 bad series |
| GET | /runs/{id}/artifacts | V | List artifacts | 200 | |
| GET | /artifacts/{id}/download | V (E for `may_contain_secrets`) | Presigned GET (5 min) | 302 | |
| POST | /runs/{id}:cancel | E | Stop traffic (≤ 10 s) | 202 | 409 INVALID_STATE |
| POST | /comparisons | V | Compare run sets `{baseline_run_ids, candidate_run_ids, scopes?}` | 200 comparison | 422 incompatible runs |
| GET | /runs/{id}/verdict | key scope `runs:read` | CI-friendly `{run_id, status, finished, passed, reason, failed_thresholds:[...], summary, new_findings:[...], url}` (see `examples/ci/README.md`) | 200 | |

### Findings, diagnosis, repair, approvals

| Method | Path | Role | Purpose | Success | Notable errors |
|---|---|---|---|---|---|
| GET | /projects/{id}/findings | V | List (status, type, severity) | 200 | |
| GET / PATCH | /findings/{id} | V / E | Detail with evidence / triage status | 200 | |
| POST | /findings/{id}/diagnoses **I** | E | Start diagnosis | 202 | 409 already running, 429 QUOTA_EXCEEDED |
| GET | /diagnoses/{id} | V | Status + ranked hypotheses + evidence | 200 | |
| POST | /hypotheses/{id}/feedback | E | `{verdict:"confirmed"|"refuted"|"unsure", note?}` | 200 | |
| POST | /hypotheses/{id}/repairs **I** | E | Propose fix `{constraints?}` | 202 | 409 max attempts, 403 org code-context disabled, 429 |
| GET | /repairs/{id} | V | Status, diff stats, gates, comparison, agent summary | 200 | |
| GET | /repairs/{id}/diff | E | Unified diff (text/x-diff) | 200 | |
| POST | /repairs/{id}:approve **I** | E | Approve verified patch → open PR | 202 | 409 INVALID_STATE (not verified) |
| POST | /repairs/{id}:reject | E | Reject with reason | 200 | |
| GET | /approvals | A | Pending approvals for org | 200 | |
| POST | /approvals/{id}:decide **I** | A (O for production) | `{decision, reason}` | 200 | 409 expired, 403 two-person rule |

### Reports

| Method | Path | Role | Purpose | Success |
|---|---|---|---|---|
| GET | /projects/{id}/summary | V | Health headline, last runs, open findings, capacity estimate | 200 |
| GET | /projects/{id}/trends | V | `?metric=p95_ms&scope=endpoint:…&from&to` per run | 200 |
| POST | /projects/{id}/reports **I** | V | Generate HTML/JSON report `{from, to, sections}` | 202 |
| GET | /reports/{id} | V | Status + presigned download + optional share link | 200 |
| GET | /projects/{id}/reports | V | Reports of the project, newest first | 200 |
| POST | /reports/{id}/share | E | `{expires_in_days ≤ 30}` → `{url, expires_at}`; the URL is shown once, replaces any earlier link | 200 |
| DELETE | /reports/{id}/share | E | Revoke the share link | 204 |
| GET | /shared/reports/{token} (no `/v1`, no auth) | — | Self-contained HTML; strict CSP, `noindex`, `no-store`; 404 when invalid, revoked or expired | 200 |

## 5. Key request/response schemas

### Register target

```http
POST /v1/projects/01J…/targets
{"base_url": "https://staging.myapp.dev", "environment": "staging"}
```
```json
201
{
  "id": "01J…",
  "host": "staging.myapp.dev",
  "environment": "staging",
  "status": "pending",
  "verification": {
    "dns_txt": {"name": "_reliability-engineer.staging.myapp.dev", "value": "re-verify=6f1c…"},
    "http_file": {"url": "https://staging.myapp.dev/.well-known/reliability-engineer.txt", "content": "re-verify=6f1c…"},
    "expires_in_hours": 72
  },
  "egress_ips": ["203.0.113.10", "203.0.113.11"]
}
```

### Create run

```http
POST /v1/projects/01J…/runs
Idempotency-Key: 4b7f…
{
  "kind": "load",
  "suite_id": "01J…",
  "load_profile_id": "01J…",
  "environment": {"type": "target_url", "target_id": "01J…"},
  "notes": "pre-launch check"
}
```
```json
202
{
  "id": "01J…",
  "status": "queued",
  "kind": "load",
  "target": {"host": "staging.myapp.dev", "environment": "staging"},
  "profile": {"type": "load", "max_vus": 1000, "duration_s": 600, "thresholds": {"p95_ms": 500, "error_rate": 0.01}},
  "quota_reservation": {"vu_minutes": 10000},
  "links": {"events": "/v1/runs/01J…/events", "self": "/v1/runs/01J…"}
}
```
Alternative `environment`: `{"type": "sandbox_build", "commit_sha": "…"}` (build the repo in
a sandbox and test it there; ADR-0011).

### Run result (abridged)

```json
{
  "id": "01J…",
  "status": "failed",
  "status_reason": "thresholds_breached",
  "summary": {
    "steady_state": {"rps": 612.4, "p50_ms": 140, "p95_ms": 1840, "p99_ms": 3900, "error_rate": 0.031},
    "thresholds": [
      {"name": "p95_ms<500", "passed": false, "observed": 1840},
      {"name": "error_rate<0.01", "passed": false, "observed": 0.031}
    ],
    "knee": {"vus": 600, "reason": "p95 crossed 500 ms"},
    "generator": {"max_cpu": 0.41, "dropped_iterations": 0, "valid": true}
  },
  "findings": [{"id": "01J…", "type": "slo_breach", "severity": "high", "title": "POST /api/orders p95 1.84 s at 600+ VUs"}]
}
```

### Diagnosis (abridged)

```json
{
  "id": "01J…",
  "status": "completed",
  "hypotheses": [
    {
      "id": "01J…",
      "rank": 1,
      "category": "pool_exhaustion",
      "statement": "POST /api/orders exhausts the Prisma connection pool (limit 10) because each order item is written in a separate transaction.",
      "mechanism": "LLM-written causal explanation …",
      "confidence": "high",
      "confidence_factors": {
        "verified_citations": 4, "evidence_kinds": ["db_stat", "trace", "metric_window", "code_location"],
        "temporal_alignment": true, "experiment": {"name": "reduce_concurrency", "result": "supports"},
        "contradicting": 0, "score": 0.82
      },
      "code_locations": [{"path": "src/app/api/orders/route.ts", "lines": [41, 63], "commit": "9c2e…"}],
      "evidence": [
        {"id": "01J…", "kind": "db_stat", "relation": "supports", "check": "verified",
         "summary": "active connections = 10/10 from t+212s; pool wait p95 1.6 s"},
        {"id": "01J…", "kind": "trace", "relation": "supports", "check": "verified",
         "summary": "trace 4bf9…: 12 sequential prisma:query spans, 1.71 s waiting on pool"}
      ]
    }
  ],
  "disclaimer": "Hypotheses are ranked explanations backed by the evidence shown; they are not guaranteed root causes."
}
```

### Repair (abridged)

```json
{
  "id": "01J…",
  "status": "verified",
  "diff_stats": {"files": 2, "additions": 38, "deletions": 21},
  "regression_tests": ["tests/reliability/orders-concurrency.spec.ts"],
  "gates": [
    {"gate": "build", "status": "passed"},
    {"gate": "existing_tests", "status": "passed", "result": {"passed": 48, "failed": 0}},
    {"gate": "fail_to_pass", "status": "passed", "result": {"base": "3/3 failed", "patched": "3/3 passed"}},
    {"gate": "functional_suite", "status": "passed"},
    {"gate": "security", "status": "passed", "result": {"new_high": 0, "new_critical": 0}},
    {"gate": "load_comparison", "status": "passed"},
    {"gate": "diff_policy", "status": "passed"}
  ],
  "comparison": [
    {"scope": "endpoint:POST /api/orders", "metric": "p95_ms", "baseline": 1840, "candidate": 310,
     "delta_pct": -83.2, "ci_pct": [-86.0, -79.5], "noise_floor_pct": 6.0, "verdict": "improved"}
  ],
  "approval": {"required": true, "state": "awaiting_approval"}
}
```

## 6. Live run events (SSE)

`GET /v1/runs/{id}/events` — `text/event-stream`; supports `Last-Event-ID` resume from
`run_events.seq` (replays from Postgres, then switches to Redis pub/sub).

```
id: 1042
event: progress
data: {"t":"2026-10-23T10:04:05Z","vus":640,"rps":588.1,"p50_ms":151,"p95_ms":620,"p99_ms":1410,"error_rate":0.004,"checks_failed":3,"generator_cpu":0.38}

id: 1043
event: status
data: {"status":"cancelling","reason":"abort_condition:error_rate"}
```
Event types: `status`, `progress` (every 2 s), `log` (runner/build lines, truncated),
`finding` (provisional), `heartbeat` (every 15 s).

## 7. Runner lease API (internal, execution plane)

Host `https://runners.<domain>/internal/runner/v1`, reachable only from the execution-plane
VPC via PrivateLink; **mTLS** with per-host certs issued at boot (short-lived, 24 h).

| Method | Path | Purpose |
|---|---|---|
| POST | /leases:acquire | Long-poll (≤ 20 s). Body `{runner_id, capacity: {cpu, mem_gb, slots}, images: [digests]}` → 200 lease or 204 |
| POST | /leases/{id}:heartbeat | Every 5 s `{progress?}` → `{action: "continue"|"cancel", extend_to}` |
| POST | /leases/{id}/events | Batched run events / progress (also published to Redis by the API) |
| POST | /leases/{id}:complete | `{outcome, exit_codes, artifacts:[{kind, key, sha256, size}], usage:{vu_minutes, sandbox_minutes}, generator_health}` |
| POST | /leases/{id}:fail | `{reason, detail}` |

Lease payload (abridged):

```json
{
  "lease_id": "…", "job_type": "load", "subject_id": "<run_id>", "org_id": "…",
  "expires_at": "…",
  "spec": {"image": "re/k6@sha256:…", "k6_options": {...}, "script_bundle_url": "<presigned GET>",
           "limits": {"cpu": 4, "mem_gb": 8, "max_wall_s": 1320}},
  "egress": {"allow_hosts": ["staging.myapp.dev"], "allow_ips": ["198.51.100.7"]},
  "secrets_sealed": "<age-encrypted to host key>",
  "tokens": {"otlp": "<run-scoped JWT, 2 h>", "events": "<lease-scoped>"},
  "uploads": {"k6_raw": "<presigned PUT>", "k6_summary": "<presigned PUT>"}
}
```

## 8. Telemetry ingest (OTLP)

`https://otlp.<domain>` (OTel Collector gateway): `/v1/traces`, `/v1/metrics`, `/v1/logs`
(OTLP/HTTP; gRPC on 4317). Auth: `Authorization: Bearer` with either a **run token**
(sandboxes, generators) or a **project ingest token** (customer staging apps, FR-23). The
gateway validates the token, **overwrites** `tenant` and `project_id` resource attributes,
applies tail sampling (keep errors, slow > p95, 5% of the rest), enforces per-org rate
limits and forwards with `X-Scope-OrgID: <org_id>` to Mimir/Tempo/Loki. Customers can never
choose the tenant header.

## 9. Webhooks

| Inbound | Path | Verification | Handling |
|---|---|---|---|
| GitHub App | `POST /webhooks/github` | `X-Hub-Signature-256` HMAC, delivery-id dedupe | installation created/deleted/suspended; repo removed; PR closed/merged (updates `pull_requests.state`); push to default branch (optional auto-discovery) |
| IdP | `POST /webhooks/idp` | provider signature | user deleted/disabled → revoke memberships, keys |

Outbound webhooks/Slack notifications are Deferred (email notifications in MVP).
