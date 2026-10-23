# Data Model

> PostgreSQL 16 schema for the control plane ([ADR-0002](decisions/0002-use-postgresql-as-the-system-of-record.md)).
> Tenancy via `org_id` + row-level security ([ADR-0003](decisions/0003-isolate-tenants-with-a-shared-schema-and-postgres-row-level-security.md)).
> This DDL is the design contract; Alembic migrations implement it in Phase 1.

## Contents
1. Conventions
2. Query patterns → indexes
3. Schema DDL
4. Row-level security
5. Concurrency-sensitive updates
6. What lives outside Postgres
7. Retention and erasure

---

## 1. Conventions

- Primary keys `uuid` (UUIDv7 generated in the app). Exceptions: `audit_events.seq`, `outbox.id` (bigserial, ordering matters).
- Every tenant-owned table has `org_id uuid NOT NULL` referencing `organizations`, and an RLS policy.
- `created_at timestamptz NOT NULL DEFAULT now()`; `updated_at` where rows are mutable.
- Enumerations are Postgres `text` + `CHECK` (cheaper to evolve than `ENUM` types).
- Soft delete only for `projects` (`archived_at`); everything else hard-deleted by retention jobs.
- Secrets, tokens and keys stored only as hashes or KMS ciphertext.
- Large/append-heavy tables (`run_events`, `audit_events`, `llm_usage`, `usage_ledger`) are range-partitioned by month (`pg_partman`).
- LangGraph checkpoint tables live in a separate schema `agent_state`, managed by the library's migrations, and also carry RLS keyed by `thread_id → org_id` mapping (`agent_threads`).

## 2. Query patterns → indexes

| # | Query | Index |
|---|---|---|
| Q1 | List my orgs | `memberships (user_id)` |
| Q2 | Authenticate API key by prefix | `api_keys (key_prefix) UNIQUE` |
| Q3 | List projects of org | `projects (org_id, created_at DESC)` |
| Q4 | Targets due for re-verification | `targets (verification_expires_at) WHERE status = 'verified'` |
| Q5 | Candidate workflows of a project by status | `workflows (project_id, status)` |
| Q6 | Run history of a project, newest first | `runs (project_id, created_at DESC)` |
| Q7 | Active runs per org / per target (admission) | `runs (org_id) WHERE status IN active`, `runs (target_id) WHERE status IN active` |
| Q8 | Rolling baseline: last 5 valid baseline runs of a profile | `runs (load_profile_id, finished_at DESC) WHERE status = 'passed' AND kind = 'load'` |
| Q9 | Run timeline | `run_events (run_id, seq)` |
| Q10 | Open findings of a project by severity | `findings (project_id, status, severity)` |
| Q11 | Upsert finding by fingerprint | `findings (project_id, fingerprint) UNIQUE` |
| Q12 | Evidence of a finding | `evidence (finding_id)` |
| Q13 | Repairs of a finding | `repair_attempts (finding_id, created_at DESC)` |
| Q14 | Pending approvals of an org | `approvals (org_id) WHERE decision IS NULL` |
| Q15 | Audit events of an org by time | `audit_events (org_id, created_at DESC)` |
| Q16 | Expired leases (reaper) | `runner_leases (expires_at) WHERE state = 'active'` |
| Q17 | Undispatched outbox | `outbox (id) WHERE dispatched_at IS NULL` |
| Q18 | Usage this period | `usage_ledger (org_id, period, kind)` |
| Q19 | Artifacts due for expiry | `artifacts (expires_at)` |

## 3. Schema DDL

```sql
-- ============ Identity & tenancy ============
CREATE TABLE plans (
  id                     text PRIMARY KEY,                 -- 'free','indie','startup','team'
  max_vus                int  NOT NULL CHECK (max_vus > 0),
  max_duration_s         int  NOT NULL CHECK (max_duration_s > 0),
  allowed_profile_types  text[] NOT NULL,
  monthly_vu_minutes     bigint NOT NULL,
  monthly_sandbox_minutes bigint NOT NULL,
  monthly_llm_tokens     bigint NOT NULL,
  max_concurrent_runs    int  NOT NULL,
  max_projects           int  NOT NULL,
  artifact_retention_days int NOT NULL DEFAULT 30
);

CREATE TABLE organizations (
  id            uuid PRIMARY KEY,
  name          text NOT NULL,
  slug          text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,40}$'),
  plan_id       text NOT NULL REFERENCES plans(id),
  llm_code_context_enabled boolean NOT NULL DEFAULT true,   -- opt-out of sending code to LLM
  suspended_at  timestamptz,                                -- abuse / kill switch per org
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY,
  idp_subject   text NOT NULL UNIQUE,
  email         citext NOT NULL,
  display_name  text,
  mfa_enabled   boolean NOT NULL DEFAULT false,              -- mirrored from IdP claims
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz
);

CREATE TABLE memberships (
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('owner','admin','engineer','viewer')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships (user_id);

CREATE TABLE api_keys (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name         text NOT NULL,
  key_prefix   text NOT NULL UNIQUE,                 -- first 12 chars, shown in UI
  key_hash     bytea NOT NULL,                       -- HMAC-SHA256(server_pepper, key)
  scopes       text[] NOT NULL,                      -- e.g. {'runs:write','runs:read'}
  project_ids  uuid[],                               -- NULL = all projects
  created_by   uuid NOT NULL REFERENCES users(id),
  expires_at   timestamptz NOT NULL,
  last_used_at timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ============ Projects, integrations, targets ============
CREATE TABLE projects (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name         text NOT NULL,
  default_slo  jsonb NOT NULL DEFAULT '{"p95_ms":500,"error_rate":0.01}',
  archived_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);
CREATE INDEX projects_org_idx ON projects (org_id, created_at DESC);

CREATE TABLE github_installations (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  installation_id   bigint NOT NULL UNIQUE,
  account_login     text NOT NULL,
  suspended_at      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE repositories (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id        uuid NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,  -- one repo per project (MVP)
  installation_id   uuid NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,
  github_repo_id    bigint NOT NULL,
  full_name         text NOT NULL,
  default_branch    text NOT NULL,
  detected_stack    jsonb,                           -- framework, language, package manager, db
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE targets (
  id                        uuid PRIMARY KEY,
  org_id                    uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id                uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  base_url                  text NOT NULL CHECK (base_url ~ '^https?://'),
  host                      text NOT NULL,           -- normalized, lowercase, punycode
  extra_hosts               text[] NOT NULL DEFAULT '{}', -- e.g. api.<host>; each verified separately
  environment               text NOT NULL CHECK (environment IN ('sandbox','preview','staging','production')),
  verification_method       text NOT NULL CHECK (verification_method IN ('dns_txt','http_file','platform')),
  verification_token_hash   bytea NOT NULL,
  status                    text NOT NULL CHECK (status IN ('pending','verified','failed','revoked')),
  verified_at               timestamptz,
  verification_expires_at   timestamptz,
  resolved_ips              inet[],                  -- last resolution; private ranges rejected
  created_by                uuid NOT NULL REFERENCES users(id),
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, host)
);
CREATE INDEX targets_reverify_idx ON targets (verification_expires_at) WHERE status = 'verified';

CREATE TABLE project_secrets (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name         text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9_]{1,63}$'),
  ciphertext   bytea NOT NULL,                       -- AES-256-GCM with per-org data key
  wrapped_dek  bytea NOT NULL,                       -- data key wrapped by KMS CMK
  created_by   uuid NOT NULL REFERENCES users(id),
  rotated_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);

CREATE TABLE openapi_specs (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source       text NOT NULL CHECK (source IN ('upload','url','repo')),
  source_ref   text,
  artifact_key text NOT NULL,
  sha256       bytea NOT NULL,
  operation_count int NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ============ Discovery, workflows, tests ============
CREATE TABLE discovery_runs (
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  commit_sha      text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  target_id       uuid REFERENCES targets(id),
  options         jsonb NOT NULL,                    -- browser_exploration, action_budget, openapi_spec_id
  status          text NOT NULL CHECK (status IN ('queued','running','awaiting_review','completed','failed','cancelled','budget_exhausted')),
  agent_thread_id text UNIQUE,
  stats           jsonb,                              -- routes_found, candidates, by_source
  error           jsonb,
  requested_by    uuid NOT NULL REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
);

CREATE TABLE workflows (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key              text NOT NULL CHECK (key ~ '^[a-z0-9_.-]{3,120}$'),
  title            text NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('api','browser')),
  criticality      text NOT NULL CHECK (criticality IN ('critical','high','normal')),
  source           text NOT NULL CHECK (source IN ('static','openapi','browser','manual')),
  discovery_confidence numeric(3,2) CHECK (discovery_confidence BETWEEN 0 AND 1),
  status           text NOT NULL CHECK (status IN ('candidate','approved','rejected','retired')),
  spec             jsonb NOT NULL,                    -- Workflow IR (validated)
  spec_version     int NOT NULL DEFAULT 1,
  destructive      boolean NOT NULL DEFAULT false,
  discovery_run_id uuid REFERENCES discovery_runs(id) ON DELETE SET NULL,
  approved_by      uuid REFERENCES users(id),
  approved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, key)
);
CREATE INDEX workflows_status_idx ON workflows (project_id, status);

CREATE TABLE test_suites (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version        int NOT NULL,
  status         text NOT NULL CHECK (status IN ('generating','validating','ready','partially_ready','failed')),
  commit_sha     text NOT NULL,
  artifact_prefix text NOT NULL,
  agent_thread_id text UNIQUE,
  generator_info jsonb NOT NULL,                      -- model ids, prompt versions, IR hashes
  created_by     uuid NOT NULL REFERENCES users(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version)
);

CREATE TABLE test_cases (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  suite_id         uuid NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
  workflow_id      uuid NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  type             text NOT NULL CHECK (type IN ('playwright','k6_scenario','k6_probe')),
  artifact_key     text NOT NULL,
  sha256           bytea NOT NULL,
  validation_status text NOT NULL CHECK (validation_status IN ('pending','valid','invalid','flaky')),
  validation_detail jsonb,
  attempts         int NOT NULL DEFAULT 0 CHECK (attempts <= 3),
  UNIQUE (suite_id, workflow_id, type)
);

CREATE TABLE load_profiles (
  id             uuid PRIMARY KEY,
  org_id         uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name           text NOT NULL,
  type           text NOT NULL CHECK (type IN ('smoke','baseline','load','stress','spike','soak')),
  executor       text NOT NULL CHECK (executor IN ('ramping-vus','ramping-arrival-rate','constant-arrival-rate')),
  stages         jsonb NOT NULL,                     -- [{"duration_s":120,"target":1000}, ...]
  max_vus        int NOT NULL CHECK (max_vus BETWEEN 1 AND 1000),   -- MVP ceiling (FR-16)
  duration_s     int NOT NULL CHECK (duration_s BETWEEN 10 AND 3600),
  think_time_s   numrange NOT NULL DEFAULT '[1,3]',
  workload_mix   jsonb NOT NULL,                     -- {"workflow_key": weight}
  thresholds     jsonb NOT NULL,                     -- {"p95_ms":500,"error_rate":0.01,"abort_error_rate":0.5}
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);

-- ============ Runs & artifacts ============
CREATE TABLE runs (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  target_id        uuid REFERENCES targets(id),       -- NULL for sandbox_env runs
  suite_id         uuid NOT NULL REFERENCES test_suites(id),
  load_profile_id  uuid REFERENCES load_profiles(id),
  kind             text NOT NULL CHECK (kind IN ('functional','load','probe','verification')),
  environment_kind text NOT NULL CHECK (environment_kind IN ('target_url','sandbox_build')),
  build_ref        jsonb,                             -- {"commit_sha":..., "repair_attempt_id":..., "variant":"baseline|patched"}
  status           text NOT NULL CHECK (status IN ('pending_approval','queued','provisioning','running','cancelling',
                                                   'analyzing','passed','failed','invalid','cancelled','errored','rejected')),
  status_reason    text,
  requested_by     uuid REFERENCES users(id),
  requested_via    text NOT NULL CHECK (requested_via IN ('ui','api_key','system')),
  api_key_id       uuid REFERENCES api_keys(id),
  idempotency_key  text,
  config_snapshot  jsonb NOT NULL,                    -- compiled k6 options / playwright config, immutable
  quota_reservation jsonb NOT NULL,
  summary          jsonb,                             -- headline metrics, verdicts
  generator_health jsonb,                             -- max_cpu, dropped_iterations
  queued_at        timestamptz,
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX runs_project_idx  ON runs (project_id, created_at DESC);
CREATE INDEX runs_active_org   ON runs (org_id)    WHERE status IN ('queued','provisioning','running','cancelling');
CREATE INDEX runs_active_tgt   ON runs (target_id) WHERE status IN ('queued','provisioning','running','cancelling');
CREATE INDEX runs_baseline_idx ON runs (load_profile_id, finished_at DESC) WHERE status = 'passed' AND kind = 'load';

CREATE TABLE run_events (                             -- partitioned monthly by created_at
  run_id      uuid NOT NULL,
  seq         bigint NOT NULL,
  org_id      uuid NOT NULL,
  type        text NOT NULL,                          -- status_changed, progress, log, abort, generator_health
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE run_metric_summaries (
  run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  org_id       uuid NOT NULL,
  scope        text NOT NULL,                         -- 'overall' | 'scenario:<key>' | 'endpoint:<METHOD path-template>'
  time_window  text NOT NULL CHECK (time_window IN ('steady_state','full')),
  requests     bigint NOT NULL,
  rps          numeric NOT NULL,
  error_rate   numeric NOT NULL,
  p50_ms numeric, p90_ms numeric, p95_ms numeric, p99_ms numeric, max_ms numeric,
  PRIMARY KEY (run_id, scope, time_window)
);

CREATE TABLE runner_leases (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL,
  job_type      text NOT NULL,                         -- functional, load, sandbox_build, sandbox_env, coding_session, security_scan
  subject_id    uuid NOT NULL,                         -- run_id / repair_attempt_id
  runner_id     text NOT NULL,                         -- host identity from mTLS cert
  state         text NOT NULL CHECK (state IN ('active','completed','expired','cancelled')),
  cancel_requested boolean NOT NULL DEFAULT false,
  expires_at    timestamptz NOT NULL,                  -- heartbeat extends (15 s window)
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX runner_leases_one_live ON runner_leases (job_type, subject_id) WHERE state = 'active';
CREATE INDEX runner_leases_expiry ON runner_leases (expires_at) WHERE state = 'active';

CREATE TABLE artifacts (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id       uuid REFERENCES runs(id) ON DELETE CASCADE,
  repair_attempt_id uuid,
  kind         text NOT NULL CHECK (kind IN ('k6_raw','k6_summary','playwright_report','playwright_trace','screenshot',
                                             'video','har','log_bundle','code_snapshot','patch','test_bundle',
                                             'report_html','report_json','scan_report')),
  storage_key  text NOT NULL UNIQUE,                   -- org/<org>/project/<p>/... (never user-chosen)
  size_bytes   bigint NOT NULL,
  sha256       bytea NOT NULL,
  sensitivity  text NOT NULL CHECK (sensitivity IN ('normal','may_contain_secrets','source_code')),
  expires_at   timestamptz NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifacts_expiry ON artifacts (expires_at);
CREATE INDEX artifacts_run    ON artifacts (run_id);

-- ============ Findings, evidence, diagnosis ============
CREATE TABLE findings (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type             text NOT NULL CHECK (type IN ('functional_failure','flaky_test','slo_breach','regression',
                                                 'saturation','error_spike','invariant_violation')),
  severity         text NOT NULL CHECK (severity IN ('critical','high','medium','low')),
  fingerprint      text NOT NULL,
  title            text NOT NULL,
  status           text NOT NULL CHECK (status IN ('open','diagnosing','diagnosed','fixing','fix_proposed','resolved','wont_fix','duplicate')),
  workflow_id      uuid REFERENCES workflows(id) ON DELETE SET NULL,
  first_run_id     uuid NOT NULL REFERENCES runs(id),
  last_run_id      uuid NOT NULL REFERENCES runs(id),
  occurrences      int NOT NULL DEFAULT 1,
  headline         jsonb NOT NULL,                      -- e.g. {"metric":"p95_ms","observed":1840,"threshold":500,"at_vus":600}
  resolved_by_run_id uuid REFERENCES runs(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, fingerprint)
);
CREATE INDEX findings_open_idx ON findings (project_id, status, severity);

CREATE TABLE evidence (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  finding_id   uuid REFERENCES findings(id) ON DELETE CASCADE,
  run_id       uuid REFERENCES runs(id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('assertion','metric_window','trace','log_excerpt','db_stat',
                                             'resource_metric','code_location','experiment','http_sample')),
  locator      jsonb NOT NULL,      -- how to re-fetch: {"promql":..,"from":..,"to":..} | {"trace_id":..} |
                                    -- {"repo_path":..,"commit":..,"lines":[a,b]} | {"artifact_id":..,"offset":..}
  summary      text NOT NULL,       -- deterministic summary (numbers, names); never LLM prose
  snapshot     jsonb,               -- small frozen copy so evidence survives telemetry retention
  collected_by text NOT NULL CHECK (collected_by IN ('detector','diagnosis_tool','experiment','user')),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX evidence_finding_idx ON evidence (finding_id);

CREATE TABLE diagnoses (
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  finding_id      uuid NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  status          text NOT NULL CHECK (status IN ('queued','collecting','hypothesizing','checking','experimenting',
                                                  'completed','inconclusive','failed','budget_exhausted')),
  agent_thread_id text UNIQUE,
  model_info      jsonb,
  token_usage     jsonb,
  requested_by    uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
);

CREATE TABLE hypotheses (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  diagnosis_id       uuid NOT NULL REFERENCES diagnoses(id) ON DELETE CASCADE,
  rank               int NOT NULL CHECK (rank >= 1),
  category           text NOT NULL CHECK (category IN ('n_plus_one','missing_index','unbounded_query','pool_exhaustion',
                       'race_condition','blocking_io','cpu_bound_handler','memory_leak','missing_cache','config_limit',
                       'external_dependency','logic_error','missing_validation','other')),
  statement          text NOT NULL,                 -- "POST /api/orders opens a DB connection per item ..."
  mechanism          text NOT NULL,                 -- causal chain, LLM-written, shown as such
  code_locations     jsonb NOT NULL,                -- [{"path":..,"lines":[..],"commit":..}]
  confidence         text NOT NULL CHECK (confidence IN ('low','medium','high')),
  confidence_factors jsonb NOT NULL,                -- rubric inputs + score (ADR-0015)
  status             text NOT NULL CHECK (status IN ('supported','weak','refuted_by_check')),
  user_verdict       text CHECK (user_verdict IN ('confirmed','refuted','unsure')),
  user_verdict_by    uuid REFERENCES users(id),
  user_verdict_note  text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (diagnosis_id, rank)
);

CREATE TABLE hypothesis_evidence (
  hypothesis_id uuid NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
  evidence_id   uuid NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  org_id        uuid NOT NULL,
  relation      text NOT NULL CHECK (relation IN ('supports','contradicts')),
  check_result  text NOT NULL CHECK (check_result IN ('verified','unverifiable','failed')),  -- deterministic citation check
  PRIMARY KEY (hypothesis_id, evidence_id)
);

-- ============ Repair & verification ============
CREATE TABLE repair_attempts (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  finding_id       uuid NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  hypothesis_id    uuid NOT NULL REFERENCES hypotheses(id),
  attempt_no       int NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
  base_commit_sha  text NOT NULL,
  constraints      jsonb NOT NULL,                  -- max_files, max_changed_lines, allowed_paths, allow_new_deps
  status           text NOT NULL CHECK (status IN ('queued','planning','coding','candidate','verifying','verified',
                                                   'verification_failed','awaiting_approval','approved','pr_opened',
                                                   'rejected','abandoned','failed','budget_exhausted')),
  patch_artifact_id uuid REFERENCES artifacts(id),
  regression_test_paths text[],
  diff_stats       jsonb,                           -- files, additions, deletions
  agent_summary    text,                            -- LLM explanation (labelled as such)
  agent_thread_id  text UNIQUE,
  token_usage      jsonb,
  requested_by     uuid NOT NULL REFERENCES users(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (finding_id, hypothesis_id, attempt_no)
);
CREATE INDEX repair_attempts_finding ON repair_attempts (finding_id, created_at DESC);

CREATE TABLE verification_gates (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  repair_attempt_id uuid NOT NULL REFERENCES repair_attempts(id) ON DELETE CASCADE,
  gate              text NOT NULL CHECK (gate IN ('build','existing_tests','fail_to_pass','functional_suite',
                                                  'security','load_comparison','invariant_probe','diff_policy')),
  status            text NOT NULL CHECK (status IN ('pending','running','passed','failed','skipped','errored')),
  applicable        boolean NOT NULL,
  run_ids           uuid[] NOT NULL DEFAULT '{}',
  result            jsonb,                          -- tool outputs summary, counts, reasons
  started_at        timestamptz,
  finished_at       timestamptz,
  UNIQUE (repair_attempt_id, gate)
);

CREATE TABLE performance_comparisons (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  repair_attempt_id uuid REFERENCES repair_attempts(id) ON DELETE CASCADE,
  baseline_run_ids  uuid[] NOT NULL,
  candidate_run_ids uuid[] NOT NULL,
  scope             text NOT NULL,                  -- same as run_metric_summaries.scope
  metric            text NOT NULL CHECK (metric IN ('p50_ms','p95_ms','p99_ms','error_rate','rps','capacity_vus')),
  baseline_value    numeric NOT NULL,
  candidate_value   numeric NOT NULL,
  delta_pct         numeric NOT NULL,
  ci_low_pct        numeric NOT NULL,
  ci_high_pct       numeric NOT NULL,
  noise_floor_pct   numeric NOT NULL,
  verdict           text NOT NULL CHECK (verdict IN ('improved','no_change','regressed','inconclusive')),
  method            text NOT NULL DEFAULT 'bootstrap_bca_10k',
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pull_requests (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  repair_attempt_id uuid NOT NULL UNIQUE REFERENCES repair_attempts(id),
  repository_id     uuid NOT NULL REFERENCES repositories(id),
  branch            text NOT NULL CHECK (branch ~ '^reliability/'),
  number            int,
  url               text,
  state             text NOT NULL CHECK (state IN ('creating','open','merged','closed','failed')),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ============ Governance ============
CREATE TABLE approvals (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  subject_type  text NOT NULL CHECK (subject_type IN ('run','repair_pr','production_target','workflow_destructive','suite_pr')),
  subject_id    uuid NOT NULL,
  summary       jsonb NOT NULL,                     -- frozen facts shown to the approver (host, env, VUs, diff stats)
  requested_by  uuid REFERENCES users(id),
  decided_by    uuid REFERENCES users(id),
  decision      text CHECK (decision IN ('approved','rejected')),
  reason        text,
  expires_at    timestamptz NOT NULL,
  decided_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (subject_type <> 'production_target' OR decided_by IS NULL OR decided_by <> requested_by)  -- two-person rule
);
CREATE INDEX approvals_pending ON approvals (org_id) WHERE decision IS NULL;

CREATE TABLE audit_events (                            -- partitioned monthly; INSERT-only grants
  seq           bigserial,
  org_id        uuid,                                  -- NULL for platform-level events
  actor_type    text NOT NULL CHECK (actor_type IN ('user','api_key','system','agent','operator','runner')),
  actor_id      text NOT NULL,
  action        text NOT NULL,                         -- 'run.started', 'target.verified', 'agent.tool_call', ...
  subject_type  text,
  subject_id    text,
  ip            inet,
  user_agent    text,
  request_id    text,
  metadata      jsonb NOT NULL DEFAULT '{}',
  prev_hash     bytea NOT NULL,
  hash          bytea NOT NULL,                        -- SHA-256(prev_hash || canonical_json(row))
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (seq, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX audit_org_time ON audit_events (org_id, created_at DESC);

CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  topic         text NOT NULL,                         -- 'run.queued', 'repair.verify', ...
  payload       jsonb NOT NULL,                        -- ids only
  org_id        uuid,
  attempts      int NOT NULL DEFAULT 0,
  dispatched_at timestamptz,
  failed_at     timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_pending ON outbox (id) WHERE dispatched_at IS NULL AND failed_at IS NULL;

CREATE TABLE idempotency_keys (
  org_id        uuid NOT NULL,
  key           text NOT NULL,
  request_hash  bytea NOT NULL,
  response      jsonb NOT NULL,
  status_code   int NOT NULL,
  expires_at    timestamptz NOT NULL,
  PRIMARY KEY (org_id, key)
);

CREATE TABLE usage_ledger (                             -- partitioned monthly
  id         uuid NOT NULL,
  org_id     uuid NOT NULL,
  period     date NOT NULL,                             -- first day of billing month
  kind       text NOT NULL CHECK (kind IN ('vu_minutes','sandbox_minutes','llm_tokens','runs')),
  quantity   bigint NOT NULL,                           -- negative = release of reservation
  entry_type text NOT NULL CHECK (entry_type IN ('reserve','commit','release','adjust')),
  subject_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE INDEX usage_period ON usage_ledger (org_id, period, kind);

CREATE TABLE llm_usage (                                -- partitioned monthly
  id            uuid NOT NULL,
  org_id        uuid NOT NULL,
  purpose       text NOT NULL,                          -- discovery.synthesize, diagnosis.hypothesize, repair.code ...
  subject_id    uuid,
  model         text NOT NULL,
  input_tokens  int NOT NULL,
  output_tokens int NOT NULL,
  cached_tokens int NOT NULL DEFAULT 0,
  latency_ms    int NOT NULL,
  outcome       text NOT NULL CHECK (outcome IN ('ok','schema_invalid','refused','error','budget_denied')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE agent_threads (                            -- maps LangGraph thread ids to tenants for RLS on agent_state.*
  thread_id   text PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  graph       text NOT NULL CHECK (graph IN ('discovery','testgen','diagnosis','repair')),
  subject_id  uuid NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

## 4. Row-level security

```sql
-- Application connects as role `app` (no BYPASSRLS). Each transaction sets the tenant:
--   SET LOCAL app.org_id = '<uuid>';
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON runs
  USING (org_id = current_setting('app.org_id')::uuid)
  WITH CHECK (org_id = current_setting('app.org_id')::uuid);
-- Same policy generated by a migration helper for every table with org_id.

-- Audit log: insert-only for app
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM app;

-- Platform jobs that must scan across tenants (reaper, retention, re-verification sweep)
-- run as role `app_system` with a narrow policy: SELECT on ids/expiry columns only, then
-- process each row inside a per-tenant transaction with app.org_id set.
```

`current_setting('app.org_id')` without a value raises an error, so a forgotten tenant
context fails closed. The contract test suite creates two orgs and asserts every endpoint
returns 404 for the other org's IDs.

## 5. Concurrency-sensitive updates

| Situation | Mechanism |
|---|---|
| Two workers advance the same run | `UPDATE runs SET status=:to WHERE id=:id AND status=:from` → 0 rows = lost race, no-op |
| Per-target single active load run | admission takes `SELECT … FOR UPDATE` on the `targets` row, then checks `runs_active_tgt` |
| Quota reservation | `pg_advisory_xact_lock(hashtext(org_id::text))` → sum ledger for period → insert `reserve` row |
| One live lease per subject | partial unique index on `(job_type, subject_id) WHERE state = 'active'`; reaper marks expired before re-lease |
| Finding upsert from parallel analyzers | `INSERT … ON CONFLICT (project_id, fingerprint) DO UPDATE SET occurrences = findings.occurrences + 1, last_run_id = EXCLUDED.last_run_id` |
| Audit hash chain | single-writer per org via `pg_advisory_xact_lock(org)` while reading last hash; acceptable at audit volumes |
| PR creation exactly once | `pull_requests.repair_attempt_id UNIQUE`; GitHub branch name derived from attempt id |

## 6. What lives outside Postgres

| Data | Store | Key / tenancy |
|---|---|---|
| Code snapshots (tar.zst), test bundles, patches, k6 raw output, Playwright traces/videos/HAR, scan reports, HTML reports | S3 bucket `re-artifacts-<env>`, SSE-KMS | `org/<org_id>/project/<project_id>/<kind>/<id>/…`; access only via presigned URLs (≤ 15 min) |
| Time-series metrics (k6, app, DB, container) | Mimir | tenant = `org_id` |
| Traces | Tempo | tenant = `org_id` |
| Logs (app-under-test, build, runner) | Loki | tenant = `org_id` |
| Live progress fan-out, rate-limit buckets, Celery broker | Redis | keys prefixed `org:<id>:` where tenant-specific; nothing authoritative |
| App secrets (DB password, GitHub App private key, IdP secrets) | AWS Secrets Manager | per service IAM |

## 7. Retention and erasure

| Data class | Default retention | Mechanism |
|---|---|---|
| Code snapshots, coding-session workspaces | ≤ 24 h after job end | S3 lifecycle + `retention.sweep` |
| Raw artifacts (traces, videos, raw k6, logs) | 30 days (plan-configurable) | `artifacts.expires_at` + S3 lifecycle |
| LGTM telemetry | 30 days | backend retention config per tenant |
| Runs, summaries, findings, evidence snapshots, hypotheses, repairs | 13 months | partition drop / batched delete |
| Audit events | 1 year (exportable) | partition drop after export |
| LLM usage records (no prompt content) | 13 months | partition drop |
| Prompt/response transcripts for evals | not stored for customer data by default; opt-in per org, 30 days | — |

Organization deletion: soft-lock → 7-day grace → delete S3 prefix, LGTM tenant data, rows
(cascade), revoke GitHub installation; audit records retained in anonymized form.
