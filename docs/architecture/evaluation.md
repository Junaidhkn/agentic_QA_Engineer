# Evaluation Methodology

> How every success criterion in the brief is measured. All numbers are **initial targets
> validated by repeatable benchmarks**, never guarantees. Benchmarks live in
> `benchmarks/` and run in staging; results are versioned and published internally per release.

## Contents
1. Principles
2. Reference environment
3. Seeded-defect benchmark corpus
4. Component metrics and gates
5. Platform SLO validation
6. Performance comparison statistics
7. Online (production) measurement
8. Reporting honesty rules

---

## 1. Principles

1. **Measure each stage separately** (discovery → test generation → detection → diagnosis →
   repair → verification) so a good end result can't hide a weak stage, and vice versa.
2. **Ground truth comes from humans or from construction** (seeded defects), never from the
   same model that produced the answer.
3. **Repeatability:** pinned reference apps, pinned sandbox specs, pinned model ids and
   prompt versions; every benchmark run records them.
4. **Regression gates in CI** for agent changes; trend dashboards for everything else.
5. **Report denominators:** "diagnosed 14 of 20 seeded defects (70%)", not "70% accurate".

## 2. Reference environment

The "controlled reference environment" named in the brief:

| Component | Spec |
|---|---|
| Load generator | 1 × k6 container, 4 vCPU / 8 GB, gVisor, same region as target |
| Target (reference API) | `benchmarks/reference-apps/ref-api` (FastAPI + Postgres, healthy version), app 4 vCPU / 8 GB, Postgres 2 vCPU / 4 GB, in `sandbox_env` |
| Workload | `ref-api-mix`: 60% reads (list/detail), 30% writes (create order), 10% search; think time uniform 1–3 s; ramping-vus 0 → 1,000 over 2 min, hold 10 min, ramp down 1 min |
| Acceptance | p95 < 500 ms, failed requests < 1% (steady-state window), generator CPU < 80%, 0 dropped iterations |

The healthy reference app is engineered to meet the SLO at 1,000 VUs; this validates
**our platform** (NFR-1), separate from finding defects in customer apps.

## 3. Seeded-defect benchmark corpus

`benchmarks/reference-apps/` holds small but realistic apps in the stacks we support
(Next.js + Prisma, Express + Drizzle/pg, FastAPI + SQLAlchemy), each with a healthy main
branch and **defect branches** created by construction, typical of AI-generated code:

| Defect class | Example injection | Expected detection |
|---|---|---|
| N+1 queries | per-item query in list endpoint | `slo_breach`/`regression` + traces |
| Missing index | drop index on `orders.user_id` | `slo_breach` + pg_stat_statements |
| Pool exhaustion | pool size 5, long transactions | `saturation` + errors under load |
| Race condition | read-modify-write stock/coupon without lock | `invariant_violation` |
| Blocking I/O | sync HTTP/file call in async handler | `slo_breach` + event-loop lag |
| Unbounded query | list endpoint without pagination | latency growth with data |
| Memory leak | module-level cache without bound | soak `saturation` |
| Missing timeout on dependency | stub dependency slows down | error spike / latency |
| Broken business logic | wrong discount math, missing auth check | `functional_failure` |
| Missing validation | 500 on malformed input | functional / error spike |

Target corpus size: 3 apps × 10 classes × 2 variants ≈ **60 labelled defects** at MVP launch,
plus a **held-out set** (20 defects) not used for prompt tuning, plus a curated set of real
open-source vibe-coded apps (with permission/licences) for discovery evaluation, with
human-labelled workflow inventories.

Each defect carries labels: workflows affected, expected finding type, root-cause category,
code location, an accepted reference fix, and a hidden "oracle" test.

## 4. Component metrics and gates

| Stage | Metric | Definition | Initial goal | CI gate (on agent/prompt change) |
|---|---|---|---|---|
| Discovery | Route recall | extracted routes ÷ labelled routes | ≥ 95% (supported frameworks) | −2 pts |
| Discovery | Critical-workflow recall | labelled critical workflows matched by a candidate ÷ labelled | ≥ 80% | −5 pts |
| Discovery | Candidate precision | candidates approved by labeller ÷ candidates | ≥ 70% | −5 pts |
| Test generation | Validity rate | test cases `valid` ÷ generated | ≥ 85% | −5 pts |
| Test generation | Flake rate | `flaky` ÷ valid, over 10 reruns | ≤ 3% | +2 pts |
| Test generation | Mutation sensitivity | functional defects caught by generated tests ÷ functional defects | ≥ 70% | −5 pts |
| Detection | Defect detection rate | seeded defects producing the expected finding type ÷ defects | ≥ 80% | −5 pts |
| Detection | False finding rate | findings on healthy branch per run | ≤ 0.1 | +0.05 |
| Diagnosis | Top-1 / Top-3 precision | correct category + location in rank 1 / top 3 ÷ diagnosed defects | ≥ 45% / ≥ 60% | −5 pts |
| Diagnosis | Citation validity | verified citations ÷ citations proposed | ≥ 90% | −5 pts |
| Diagnosis | Calibration | accuracy within each confidence band (high should be ≥ 80% correct) | monotonic bands | band inversion fails |
| Diagnosis | Inconclusive rate | inconclusive ÷ diagnoses | tracked | — |
| Repair | Verified rate | attempts reaching `verified` ÷ attempts | ≥ 40% at launch | −5 pts |
| Repair | Oracle pass rate | verified patches that also pass the hidden oracle test | ≥ 90% | −5 pts |
| Repair | False-verified rate | verified patches failing oracle or judged wrong in blind human review | < 5% | any increase blocks |
| Repair | Patch size | median changed lines | ≤ 60 | — |
| Repair | Perf improvement | median p95 reduction on perf defects (verified patches) | report | — |
| Cost | Tokens per stage | median and p90 tokens per discovery / diagnosis / repair | within budgets | +20% blocks |

Blind human review: each release, two engineers review a random sample (≥ 20) of verified
patches without seeing the agent's explanation, rating correct / acceptable / wrong.

## 5. Platform SLO validation

| Criterion (brief) | How measured | Pass condition | When |
|---|---|---|---|
| 1,000 VUs for 10 min | reference workload (§2) × 5 repetitions | all 5 meet acceptance; generator CPU < 80%, 0 dropped iterations | each release; nightly smoke at 1,000 VUs |
| p95 < 500 ms, < 1% failures | same runs, steady-state window | p95 and failure rate within targets in all 5 | each release |
| Execute all approved critical workflows; report discovered vs tested | coverage report on corpus apps | 100% of approved critical workflows have a valid test executed in the last suite run, or an explicit `invalid` reason shown | each release |
| Evidence on every root cause | DB constraint + check: every displayed hypothesis has ≥ 1 verified supporting citation | 100% (hard invariant, monitored in prod) | continuous |
| Diagnosis precision vs verified incidents | §4 + online confirmed/refuted feedback | top-3 ≥ 60% benchmark; online tracked | each release / monthly |
| Repairs verified only after all gates | invariant: `verified` status requires all applicable gates `passed` (enforced in code + DB check job) | 100% | continuous |
| 99.9% control-plane availability | SLO from synthetic checks + ALB 5xx | ≥ 99.9% monthly | monthly |
| 95% jobs start < 30 s | `job_start_seconds` at reference platform load: replay of 25 concurrent load runs + 100 functional runs/hour in staging | p95 < 30 s | each release (load test of the platform) |
| Zero unauthorized production load tests / auto-deploys | admission audit; egress proxy logs vs verified hosts; absence of merge/deploy code paths (test asserts App has no merge calls) | 0 violations; any egress to non-allowlisted host pages | continuous |
| Kill ≤ 10 s | chaos test: stop during 1,000-VU run, measure last request timestamp at target | 100% ≤ 10 s | nightly |

### As built (Phase 10): how each row is checked today

| Criterion | Automated check | Status |
|---|---|---|
| 1,000 VUs for 10 min; p95 < 500 ms, < 1 % failures | NFR-1 reference workload on a 4 vCPU generator | **Not yet run** — needs the staging execution plane (gVisor hosts); the k6 path is exercised end to end at small scale in CI |
| Approved critical workflows executed; discovered vs tested | coverage report (`/projects/{id}/summary`, report `coverage` section) | In CI on the corpus apps (Phase 5, 9 tests) |
| Evidence on every root cause | invariant `hypothesis_without_verified_support` (hourly `ops.integrity`, pages on > 0) + save/read filters | Continuous |
| Diagnosis precision | `tests/benchmark` (diagnosis) | 6/6 top-1 on the seeded corpus (Phase 7); online feedback tracked |
| Repairs verified only after all gates | invariant `verified_repair_without_passed_gates` (hourly) + pipeline tests | Continuous |
| 99.9 % availability | `re_http_requests_total` burn-rate alerts + canary every 15 min + ALB 5xx alarm | Instrumented; the monthly number needs production traffic |
| 95 % jobs start < 30 s | `re_job_start_seconds` SLI + burn-rate alerts; `benchmarks/harness/platform_load.py` | Scaled run in CI (3 lanes, 4 runners: p95 ≈ 4 s); the 25-concurrent replay needs staging |
| Zero unauthorized production load tests / auto-deploys | admission tests; invariant `pull_request_without_approval`; architecture test (no merge/deploy code paths) | Continuous |
| Kill ≤ 10 s | `re_kill_latency_seconds` (runner-measured) + `KillLatencyBreach` page; chaos/e2e tests stop a running load run | In CI at 3 VUs; the 1,000-VU nightly chaos run needs staging |
| Backups restorable | weekly `restore-drill` (RDS) recording `restore_drill`; dump-mode drill in CI | Dump drill in CI; RDS drill runs once staging exists |

## 6. Performance comparison statistics

Used for regression findings and the `load_comparison` gate ([ADR-0016](decisions/0016-gate-verified-status-on-independent-checks-and-statistical-load-comparison.md)).

1. **Design:** same sandbox spec, same data seed, same workload; runs interleaved
   `B P B P B P` (≥ 3 each) to cancel drift; first 60 s after ramp excluded.
2. **Statistic:** per run, the steady-state p95 per scope (overall and affected endpoints).
3. **Estimate:** relative difference of medians-of-p95 between P and B; 95% **BCa bootstrap**
   confidence interval over runs (resampling request latencies within runs, stratified by run).
4. **Noise floor:** per project and environment, the CI half-width observed in A/A runs (B
   vs B), refreshed weekly; default 5% until measured.
5. **Verdict:** `improved` if CI upper bound < −noise_floor; `regressed` if lower bound >
   +noise_floor; `no_change` if CI within ±noise_floor; else `inconclusive` (more runs
   suggested). Error rate compared with a two-proportion test; must not be worse.
6. For **customer target URLs** (not sandboxes), the same method applies but results are
   labelled "environment not controlled" and require ≥ 5 runs per side.

## 7. Online (production) measurement

| Metric | Source |
|---|---|
| Onboarding success: % of projects reaching first successful run (by stack) | runs |
| Sandbox build success rate by stack | runs (`errored: build_failed`) |
| Hypothesis feedback: confirmed / refuted / unsure rates by category and confidence band | hypotheses.user_verdict |
| Repair acceptance: approved ÷ verified; PR merged ÷ opened; reverted within 30 days | repair_attempts, GitHub webhooks |
| Time to first finding; time from finding to verified patch | timestamps |
| Cost per org per month vs plan price | usage_ledger, llm_usage |
| Safety: admission rejections by code, abuse queue outcomes | audit |

Monthly review: compare online diagnosis precision and repair acceptance with benchmark
numbers; a gap > 15 points triggers corpus expansion for the under-represented class.

## 8. Reporting honesty rules

Product copy and reports must:
- show coverage as counts of discovered/approved/tested workflows, never "100% coverage";
- label hypotheses as hypotheses with their confidence band and evidence;
- show "verified" only with its gate list and comparison CIs;
- state when results came from an uncontrolled environment or an `invalid` run;
- never describe a repair as guaranteed or the analysis as complete.
