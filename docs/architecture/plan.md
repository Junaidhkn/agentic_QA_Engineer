# Implementation Plan and Roadmap

> Written at Gate 7. **Implementation begins only after explicit approval.**
> Every file references the requirement (FR/NFR) or ADR it implements. Paths follow
> [low-level-design.md §1](low-level-design.md#1-repository-layout).

**Approved:** yes — 2026-10-23 (user: "approved, start phase 0 and do not stop until all are complete")

## Roadmap at a glance

| Milestone | Ends after | What a user can do | Audience |
|---|---|---|---|
| **M1 — Load-test alpha** | Phase 4 | Sign up, connect repo, verify a staging URL, define workflows (manual/OpenAPI), run smoke/baseline/load up to 1,000 VUs, watch live, get findings with evidence | internal + 3–5 design partners |
| **M2 — Discovery & diagnosis beta** | Phase 7 | Automatic route discovery + generated Playwright/k6 suites; sandbox builds; evidence-cited hypotheses | private beta (~20 orgs) |
| **M3 — Repair beta** | Phase 9 | Patches + regression tests, verification gates, baseline-vs-patched comparisons, approved PRs; reports; CI gating | private beta |
| **M4 — Public MVP** | Phase 11 | Hardened, benchmarked, pen-tested; SLOs met for a full month in beta | public |

Rough sizing assumes 4 engineers; ranges are planning estimates, not commitments.

## Phase overview

| # | Phase | Depends on | Est. (eng-weeks) | Status |
|---|---|---|---|---|
| 0 | Foundation and vendor choices | — | 4 | ☑ |
| 1 | Data layer, tenancy, platform core | 0 | 5 | ☑ |
| 2 | Identity, orgs, projects, GitHub, targets, secrets | 1 | 6 | ☑ |
| 3 | Execution plane and run pipeline (vertical slice) | 2 | 10 | ☑ |
| 4 | Telemetry, detection, findings, live dashboard → **M1** | 3 | 6 | ☑ |
| 5 | LLM gateway, discovery, Workflow IR, test generation | 4 | 9 | ☑ |
| 6 | Sandbox environments (build + app-under-test) | 3, 5 | 8 | ☑ |
| 7 | Diagnosis → **M2** | 4, 6 | 8 | ☐ |
| 8 | Repair, verification gates, approvals, PRs | 6, 7 | 10 | ☐ |
| 9 | Reports, coverage, CI integration, browser exploration → **M3** | 8 | 6 | ☐ |
| 10 | Hardening, benchmarks, operations readiness | 9 | 6 | ☐ |
| 11 | Private-beta exit and public launch → **M4** | 10 | 4 | ☐ |

---

## Phase 0 — Foundation and vendor choices
**Goal:** a repo where every later phase can add code with CI, local environment and decided vendors.
**Files to create**
- `README.md`, `CONTRIBUTING.md` — repo overview, dev setup — ADR-0001
- `services/control-plane/pyproject.toml`, `uv.lock` — Python project — ADR-0001
- `services/control-plane/src/reliability/platform/settings.py` — typed config from env — design §8
- `services/control-plane/src/reliability/main_api.py` — FastAPI app factory, health/readiness — ADR-0001
- `services/control-plane/src/reliability/main_worker.py` — Celery app, queues `default/agents/github` — ADR-0004
- `apps/web/` (Next.js App Router skeleton, Tailwind, lint/test config) — ADR-0022
- `packages/api-client/` (openapi-typescript generation script) — api-contract §1
- `deploy/local/docker-compose.yml` — Postgres, Redis, MinIO, LGTM, fake GitHub, fake LLM — operations §2
- `.github/workflows/ci.yml` — lint, typecheck, tests, scans, SBOM — operations §2
- `.importlinter` — module boundary contracts — ADR-0001
- `.env.example` — required variables — design §8
- `docs/architecture/vendors.md` — IdP, email, error-tracking choices + LLM/cloud price sheets → cost model — ADR-0018, ADR-0019, ADR-0020
**Interfaces introduced**
- Error envelope + exception mapping; request-id middleware.
**Tests**
- health endpoints; CI runs green on empty modules.
**Decisions involved:** ADR-0001, 0004, 0018, 0019, 0020, 0022
**Tooling:** GitHub MCP (installed) for issues per phase; `claude-api` skill (available) for vendor/model notes.
**Done when:** `docker compose up` + `make test` pass locally and in CI; vendor choices recorded.

## Phase 1 — Data layer, tenancy, platform core
**Goal:** schema, RLS, outbox, audit and quota primitives every module uses.
**Files to create**
- `services/control-plane/migrations/versions/0001_core.py` … — DDL from data-model.md — ADR-0002
- `src/reliability/platform/db.py` — async engine, session-per-request, `SET LOCAL app.org_id` — ADR-0003
- `src/reliability/platform/rls.py` — migration helper generating policies; CI check script — ADR-0003
- `src/reliability/platform/outbox.py` + `main_relay.py` — transactional outbox + relay — ADR-0004
- `src/reliability/modules/audit/` — hash-chained writer, anchor job — FR-29, ADR-0023
- `src/reliability/modules/quotas/` — plans, ledger reservations, Redis token buckets — FR-31, ADR-0024
- `src/reliability/platform/idempotency.py` — Idempotency-Key middleware — api-contract §1
- `src/reliability/platform/storage.py` — `ObjectStore` adapter, key builder, presign — ADR-0005
**Interfaces introduced**
- `audit.record(event)`, `quotas.reserve/commit/release`, `ObjectStore.presign_put/get`, `outbox.enqueue(topic, payload)`.
**Tests**
- RLS: two orgs, every table, cross-tenant reads/writes fail (NFR-6).
- Outbox: rollback → no dispatch; crash → redelivery once (ADR-0004).
- Quota: parallel reservations never exceed limit (property test).
- Audit chain verification and tamper detection.
**Decisions involved:** ADR-0002, 0003, 0004, 0005, 0023, 0024
**Done when:** all core tables migrate up/down on a fresh DB; tenancy suite green.

## Phase 2 — Identity, orgs, projects, GitHub, targets, secrets
**Goal:** UF-1 onboarding end to end.
**Files to create**
- `modules/identity/` — IdP JWT verification adapter, API keys — FR-1, FR-30, FR-32, ADR-0018
- `modules/tenancy/` — orgs, memberships, invitations, `require(role, resource)` — FR-1, FR-30
- `modules/projects/` — FR-2
- `modules/integrations/github/` — App install flow, webhook HMAC, token minting, snapshot download — FR-2, ADR-0017
- `modules/targets/` — registration, SSRF checks, DNS/HTTP verification, re-verify sweep, environment policy — FR-3…5, ADR-0010
- `modules/secrets/` — KMS envelope encryption, write-only API — FR-11
- `apps/web/app/(app)/onboarding/*`, `settings/*` — UF-1 screens
**Interfaces introduced**
- `targets.assert_runnable(target, profile)`; `github.mint_token(job)`; `github.snapshot(repo, sha) -> artifact`.
**Tests**
- Verification: DNS and file methods, expiry, private-IP rejection, redirect handling (FR-3).
- Role matrix contract tests (FR-30); webhook signature tests.
**Decisions involved:** ADR-0010, 0017, 0018
**Tooling:** GitHub MCP for creating a test GitHub App installation repo (proposed).
**Done when:** a new user can sign in with GitHub, create a project, link a repo and verify a staging host.

## Phase 3 — Execution plane and run pipeline (vertical slice)
**Goal:** a manual workflow becomes a k6 load run and a Playwright smoke run against a verified target, safely.
**Files to create**
- `infra/terraform/execution-plane/` — account, VPC, ASG + warm pool, egress proxy, NAT EIPs, PrivateLink — ADR-0008, 0009, 0020
- `infra/packer/runner-host.pkr.hcl` — Docker + gVisor + hardening — ADR-0009
- `services/runner-agent/` — lease loop, container driver (runsc), per-run networks, egress programming, heartbeats, kill, artifact upload — ADR-0008, FR-17…19
- `runners/images/k6/`, `runners/images/playwright/` — pinned, signed images — ADR-0012
- `modules/runners/` — lease API (mTLS), reaper — api-contract §7, NFR-4
- `modules/admission/` — ordered admission function — FR-14, ADR-0024
- `modules/runs/` — state machine, events, cancel, kill switch — FR-14…18
- `modules/loadprofiles/` — profile CRUD + k6 options compiler (thresholds, abortOnFail) — FR-11, FR-16, FR-17
- `modules/workflows/` (manual IR only) + `packages/workflow-ir/schema.json` — ADR-0013
- `modules/approvals/` — run approvals (first load, > 500 VUs, production smoke) — FR-5
- `apps/web/app/(app)/projects/[id]/{runs,profiles,workflows}` — UF-3, UF-4 screens
**Interfaces introduced**
- Lease protocol v1; `admission.admit(request) -> Admitted | Rejected(code)`; run state machine transitions.
**Tests**
- Runner integration (real gVisor host, nightly): egress denial, metadata blocked, kill ≤ 10 s, resource limits (NFR-7).
- Admission property tests; production policy (FR-5).
- k6 under gVisor vs runc benchmark at 1,000 VUs (ADR-0009 follow-up, NFR-1).
**Decisions involved:** ADR-0008, 0009, 0010, 0012, 0013, 0024
**Tooling:** AWS/Terraform docs MCP (proposed).
**Done when:** 1,000-VU, 10-min run against the reference app completes with valid generator health; Stop halts traffic ≤ 10 s.

## Phase 4 — Telemetry, detection, findings, live dashboard → M1
**Goal:** live charts, run summaries, deterministic findings with evidence.
**Files to create**
- `infra/terraform/observability/` + `deploy/otel-gateway/config.yaml` — OTLP gateway, tail sampling, tenant header — ADR-0014
- LGTM deployment (single-binary Mimir/Tempo/Loki on S3) — ADR-0014
- `modules/telemetry/` — `TelemetryQuery` adapter (PromQL/TraceQL/LogQL with tenant) — FR-15, FR-23
- `modules/runs/analysis.py` — steady-state window, summaries, generator validity — FR-19
- `modules/findings/` — detectors, fingerprints, baselines, evidence attachment — FR-20
- SSE endpoint + Redis pub/sub — FR-15, ADR-0021
- `apps/web/.../runs/[runId]`, `findings`, `findings/[fid]` (evidence only) — UF-4, UF-5
- Customer OTLP ingest tokens (project settings) — FR-23
**Tests**
- Detector unit tests on recorded k6 outputs; baseline regression math; SSE resume.
**Decisions involved:** ADR-0014, 0021, 0022
**Done when (M1):** design partners run load tests on their staging apps and get findings with evidence; NFR-1 benchmark passes on reference env.

## Phase 5 — LLM gateway, discovery, Workflow IR, test generation
**Goal:** UF-2 and UF-3 automated: discovered candidates → approved → generated, validated suites.
**Files to create**
- `src/reliability/llm/` — gateway: purpose routing, budgets, redaction, schema validation, caching, `llm_usage` — ADR-0019
- `agents/common/` — budgets, tool registry, untrusted-content wrapper, checkpointer wiring — ADR-0006, ADR-0007
- `agents/discovery/` — graph, route adapters (Next.js, Express, FastAPI), OpenAPI import — FR-6, FR-7, FR-9
- `agents/testgen/` — graph, templates, static checks, dry run, flake check — FR-10, FR-12, FR-13
- `modules/discovery/`, `modules/testsuites/`, `modules/workflows/` (candidates, review, coverage) — FR-9
- `benchmarks/reference-apps/` (3 stacks, healthy + first defect branches) and `benchmarks/harness/` — evaluation §3
- `.github/workflows/evals.yml` — agent eval gate — evaluation §4
- `apps/web/.../discovery`, `workflows` (review UI, IR editor), `suites`
**Tests**
- Route extraction recall on labelled apps; IR validation; generated test validity/flake metrics; prompt-injection fixtures in repos.
**Decisions involved:** ADR-0006, 0007, 0013, 0019
**Tooling:** `claude-api` skill (available) for gateway implementation; Playwright MCP (proposed) to inspect reference apps while writing templates.
**Done when:** on the corpus, route recall ≥ 95% and test validity ≥ 85% (evaluation §4).

## Phase 6 — Sandbox environments
**Goal:** build and run the customer app in a controlled, instrumented sandbox (ADR-0011).
**Files to create**
- `runners/images/build/` — buildpacks/Dockerfile/compose builders with layer cache — ADR-0011
- `runners/images/otel-autoinstrument/` — Node/Python zero-code instrumentation injection — FR-23
- `services/runner-agent/sandbox_env/` — compose rewriting to runsc, ephemeral DB, migrations, seed, stub server, exporters — ADR-0011
- Package mirror (npm/PyPI proxy) Terraform — threat-model A6
- `modules/runs` support for `environment_kind = sandbox_build` — FR-25 prerequisite
**Tests**
- Build success matrix over reference apps and 20 public sample apps; DB seeding; stub routing; telemetry presence.
**Decisions involved:** ADR-0009, 0011
**Done when:** ≥ 80% of corpus apps build and serve in sandbox; traces and DB stats visible for runs.

## Phase 7 — Diagnosis → M2
**Goal:** UF-5 hypotheses with verified evidence and rubric confidence.
**Files to create**
- `agents/diagnosis/` — evidence pack collectors, read-only tools, hypothesis schema, citation checker, experiments, rubric — FR-21, FR-22, ADR-0015
- `modules/diagnosis/` — API, persistence, feedback — FR-22
- `apps/web/.../findings/[fid]` — hypotheses, confidence factors, evidence deep links, feedback
**Tests**
- Citation checker unit tests (numeric tolerance, missing evidence); rubric calibration on corpus; top-3 precision gate.
**Decisions involved:** ADR-0007, 0015
**Done when (M2):** top-3 precision ≥ 60% on the corpus; 100% of displayed hypotheses have verified citations.

## Phase 8 — Repair, verification gates, approvals, PRs
**Goal:** UF-6 end to end.
**Files to create**
- `runners/images/coding-tools/` — repo checkout, allowlisted commands — agent-workflows §6
- `agents/repair/` — graph, sandbox tools, diff policy pre-check — FR-24, ADR-0007
- `modules/verification/` — gate pipeline, comparison statistics (BCa bootstrap), noise-floor jobs — FR-25, FR-26, ADR-0016
- `runners/images/security-scan/` — semgrep, osv-scanner, gitleaks, ZAP baseline — FR-25
- `modules/repairs/`, `modules/approvals/` (repair PR approvals), `integrations/github/pr.py` (template PR) — FR-27, ADR-0017
- `apps/web/.../repairs/[rid]` — diff viewer, gates, comparison, approve/reject
**Tests**
- Gate pipeline integration on corpus defects; fail-to-pass logic; statistics property tests (A/A yields no_change); no merge code path test; prompt-injection repo suite.
**Decisions involved:** ADR-0007, 0011, 0016, 0017
**Done when:** verified rate ≥ 40%, oracle pass ≥ 90%, false-verified < 5% on corpus.

## Phase 9 — Reports, coverage, CI integration, browser exploration → M3
**Files to create**
- `modules/reports/` — trends, coverage, HTML/JSON export, share links — FR-28
- `/v1/runs/{id}/verdict` + GitHub Action example `examples/ci/reliability-check.yml` — FR-32
- `agents/discovery/explore_ui.py` — bounded browser exploration with denylist — FR-8
- `apps/web/.../reports`, `/approvals`, `settings/usage`, `settings/audit`
**Tests:** report correctness snapshots; CI verdict contract; exploration never triggers denylisted actions on fixture app.
**Decisions involved:** ADR-0013, 0022
**Done when (M3):** design partners complete UF-1…UF-7 unaided.

## Phase 10 — Hardening, benchmarks, operations readiness
**Files to create**
- `infra/terraform/control-plane/` production sizing, WAF, alarms — operations §1, §6
- `docs/runbooks/*.md` — operations §9
- SLO dashboards + burn-rate alerts; synthetic canary org — NFR-3, NFR-4
- Restore drill automation — NFR-9
- Platform load test (25 concurrent load runs + 100 functional/hour) — NFR-4
**Tests:** external penetration test; chaos game day; full evaluation.md §5 table.
**Done when:** every row in evaluation.md §5 passes on staging; pen-test highs fixed.

## Phase 11 — Private-beta exit and public launch → M4
- Plans and limits finalized; ToS/AUP (load-testing consent), DPA, sub-processor list — NFR-14
- Abuse review queue and operator tools — threat-model A1
- One month of beta meeting NFR-3/NFR-4; launch checklist.

## Post-MVP roadmap (Deferred, with triggers)

| Item | Trigger |
|---|---|
| Kubernetes + k6-operator, > 1,000 VUs, distributed generators | sustained > 20 hosts or customer need > 1,000 VUs |
| Multi-region generators | customers needing geo-distributed load |
| Production smoke/canary monitoring and production load windows | enterprise customer with change-management process |
| Supabase/Firebase local emulation | > 30% onboarding blocked by BaaS |
| More frameworks (Django, Rails, Laravel, Remix, SvelteKit) | demand per stack |
| GitLab / Bitbucket | customer demand |
| SSO/SAML, SCIM | team-plan requirement |
| Stripe billing and metering | paid launch |
| Self-hosted runners (customer VPC) | customers unable to expose staging |
| Browser-based performance under load | demand for front-end metrics |
| Customer-facing Grafana / ad-hoc queries | power-user demand |

## Deviations log
| Date | Phase | Deviation | Reason |
|---|---|---|---|
| 2026-10-23 | 1 | RLS keys on a per-transaction list `app.org_ids` (all orgs the caller belongs to) instead of a single `app.org_id`; role checks per resource happen in the policy layer | Project-scoped URLs don't carry the org; RLS still fails closed and limits every query to the caller's orgs |
| 2026-10-23 | 1 | Cross-tenant jobs use a `re_system` role with BYPASSRLS, restricted to an allowlist of modules enforced by a test, instead of column-limited policies | Simpler, same blast-radius control; usage is test-enforced |
| 2026-10-23 | 1 | Partitioned tables use a DEFAULT partition plus a daily job creating future monthly partitions (no pg_partman) | Works on any Postgres without extensions |
| 2026-10-23 | 1 | Added tables: invitations, feature_flags, ingest_tokens, execution_jobs, reports, abuse_reviews, audit_anchors | Needed by API endpoints/jobs already in the design |
| 2026-10-23 | 2 | GitHub install callback proves installer access via the App's OAuth user token (`RE_GITHUB_CLIENT_ID/SECRET`); without OAuth configured the callback is accepted only in local/test | Prevents linking someone else's installation by replaying an installation id |
| 2026-10-23 | 2 | Added `RE_ALLOW_PRIVATE_TARGETS` (local only, rejected at startup in staging/production) | Lets developers test the platform against apps on 127.0.0.1 |
| 2026-10-23 | 3 | Test code is compiled **deterministically** from Workflow IR (generated files embed the IR as JSON and call a fixed k6/Playwright runtime); the LLM's role in test generation is to produce and repair IR, not to write code | Reproducible, injection-safe, reviewable; stays within ADR-0013's intent (IR is the contract) |
| 2026-10-23 | 3 | Egress enforcement uses a per-run Smokescreen proxy container on an `--internal` Docker network plus NAT gateways with Elastic IPs, instead of a shared egress-proxy fleet | Per-run allowlists with no shared state; fewer moving parts; same guarantees |
| 2026-10-23 | 3 | Runner identity = signed runner token (24 h) checked by the app in addition to PrivateLink/mTLS; lease secrets sealed with X25519 to a per-process runner key | Defense in depth; keys never leave the runner process |
| 2026-10-23 | 3 | Download endpoints return `{url}` JSON instead of a 302 | Browser clients authenticate with bearer tokens, which redirects can't carry |
| 2026-10-23 | 3 | Runner uploads per-run 5 s time-series with mergeable log-scale latency histograms and bounded request samples; raw k6 output only when < 64 MB compressed | Raw output of a 1,000-VU run is ~1 GB; histograms keep steady-state percentiles exact enough (±2.5%) |
| 2026-10-23 | 4 | OTLP ingest is an authenticating gateway (`reliability.otlp_gateway`) in front of an internal OTel Collector that keeps `X-Scope-OrgID` as client metadata and batches per tenant; tail sampling replaced by 10% head sampling via traceparent flags | Tail sampling loses per-tenant client metadata in the collector; head sampling keeps tenancy simple and correct |
| 2026-10-23 | 4 | Live view uses fetch-based SSE (Authorization header) instead of `EventSource` | `EventSource` cannot send bearer tokens; avoids tokens in URLs |
| 2026-10-23 | 4 | Steady-state metrics, knee detection and charts are computed from the runner's time-series artifact; LGTM is used for server-side evidence (traces/logs/DB) | k6 metrics are complete without LGTM; keeps M1 independent of telemetry backends |
| 2026-10-23 | 5 | Discovery ends at `persist` with status `awaiting_review`; human review happens through the workflows API/UI instead of a graph `interrupt()` | Review is per-workflow and can take days; candidates are durable rows, so no thread needs to stay open |
| 2026-10-23 | 5 | Route extraction uses Python `ast` (FastAPI) and structural pattern adapters (Express mounts/routers, Next.js App and Pages Router files) instead of tree-sitter | No native dependency; 100% route recall on the labelled corpus. Tree-sitter remains the upgrade path when the corpus shows misses |
| 2026-10-23 | 5 | Test generation graph: deterministic compile → runner dry-run ×3 (`test_validation` job, graph waits via `interrupt`) → model repairs the **IR** (≤ 3 attempts, endpoints limited to known ones, destructive flag cannot be dropped); `static_check` (tsc/eslint/k6 inspect) dropped | Code comes from a fixed runtime, so static checks add nothing; dry runs catch real failures |
| 2026-10-23 | 5 | Every LLM step has a deterministic baseline (resource journeys, auth journeys, page loads; criticality rules) and degrades to it on refusal, outage, opt-out or budget/quota exhaustion | Keeps discovery useful without a model and bounds the blast radius of model failures |
| 2026-10-23 | 5 | Generated API workflows that need auth prepend sign-up + log-in steps when no `TEST_USER_EMAIL/PASSWORD` secrets exist; with secrets a login precondition is used | Makes generated tests self-contained for apps with open registration |
| 2026-10-23 | 5 | Added `POST /projects/{id}/source-archives` (zip/tar.gz) as a discovery source besides GitHub | Local development, evaluation harness and customers who can't install the GitHub App yet |
| 2026-10-23 | 5 | Reference apps carry seeded defects as `DEFECTS=…` toggles (documented in `labels.json`) instead of defect branches; corpus starts with 3 apps (FastAPI, Express, Next.js) | One tree to maintain; the harness and later diagnosis/repair benchmarks switch defects at runtime |
| 2026-10-23 | 5 | Claude calls use server-side refusal fallbacks (`fallbacks="default"`), adaptive thinking with explicit effort per purpose, structured outputs for closed schemas and prompt-embedded schemas + validation/re-ask for open ones (Workflow IR) | Constrained decoding needs closed schemas; IR is an open schema validated by our own validator |
| 2026-10-23 | 5 | LangGraph checkpoints live in the `agent_state` schema (migration 0002), reached only by the system role; `agent_threads` (RLS) maps threads to tenants | Checkpoint tables are created by the saver itself; tenant data stays behind RLS |
| 2026-10-24 | 6 | A sandboxed run is one lease (fetch source → build → start → migrate/seed → health → test → collect → teardown) instead of separate `sandbox_build` and `sandbox_env` jobs; a standalone `sandbox_build` job type checks buildability and shows the plan | Fewer moving parts and no cross-job hand-off of running containers; same isolation |
| 2026-10-24 | 6 | Build plans are computed deterministically by the control plane (`modules/environments/plan.py`) and shown before anything runs; template strategy uses platform Dockerfile templates (exec-form commands) instead of Cloud Native Buildpacks | Reviewable plans, works offline against the package mirror, no buildpack builder images to maintain |
| 2026-10-24 | 6 | Process sandbox for local development and tests: app runs as host processes, ephemeral database on `RA_SANDBOX_POSTGRES_URL`, optional inherited Python packages; refused outside local/test like the process driver | Lets the whole sandbox path (incl. migrations, seed, DB evidence) be tested without Docker |
| 2026-10-24 | 6 | Database evidence comes from SQL snapshots taken by the runner (`pg_stat_database`, `pg_stat_user_tables/indexes`, `pg_stat_statements` when available) plus connection samples every 5 s, stored in the run's sandbox report and a `telemetry_snapshot` artifact; postgres_exporter/cAdvisor deferred | Same evidence for diagnosis with no extra containers; exporters can be added when time series are needed |
| 2026-10-24 | 6 | Package mirror is AWS CodeArtifact (npm + PyPI upstreams) behind interface endpoints, read-only for runner hosts | Managed, auditable, no mirror servers to run |
| 2026-10-24 | 6 | Stub server covers dependencies whose base URL comes from an environment variable; hard-coded external hosts are reported as plan warnings (no TLS interception); seeds come from the project's own seed scripts (no generated synthetic seed yet) | Predictable behaviour; synthetic seeding is follow-up work driven by onboarding data |
| 2026-10-24 | 6 | App secrets for sandboxes are project secrets named `SANDBOX_<NAME>`; other required settings get placeholders listed in the plan | Explicit opt-in, never mixes test-user credentials with app configuration |
| 2026-10-24 | 6 | Build-success matrix measured over the reference corpus (process sandbox, CI `evals.yml`); the 20-public-app matrix moves to Phase 10 benchmarks on a Docker-capable CI runner | No Docker in this CI; Docker paths are covered by command/compose construction tests |
