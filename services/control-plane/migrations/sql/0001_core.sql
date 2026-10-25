-- Core schema (docs/architecture/data-model.md). Executed by migration 0001 as the schema owner.
CREATE EXTENSION IF NOT EXISTS citext;

-- ============ Identity & tenancy ============
CREATE TABLE plans (
  id                      text PRIMARY KEY,
  max_vus                 int  NOT NULL CHECK (max_vus > 0),
  max_duration_s          int  NOT NULL CHECK (max_duration_s > 0),
  allowed_profile_types   text[] NOT NULL,
  monthly_vu_minutes      bigint NOT NULL,
  monthly_sandbox_minutes bigint NOT NULL,
  monthly_llm_tokens      bigint NOT NULL,
  max_concurrent_runs     int  NOT NULL,
  max_projects            int  NOT NULL,
  artifact_retention_days int  NOT NULL DEFAULT 30,
  features                jsonb NOT NULL DEFAULT '{}'
);

INSERT INTO plans VALUES
 ('free',    50,   300, '{smoke,baseline}',                              500,    300,    2000000,  1,  1, 7,  '{"repair":false,"browser_exploration":false}'),
 ('indie',   500,  1800,'{smoke,baseline,load,stress,spike}',           20000,  3000,   30000000, 2,  3, 30, '{"repair":true,"browser_exploration":true}'),
 ('startup', 1000, 3600,'{smoke,baseline,load,stress,spike,soak}',      100000, 15000,  150000000,4,  10, 30, '{"repair":true,"browser_exploration":true}'),
 ('team',    1000, 3600,'{smoke,baseline,load,stress,spike,soak}',      400000, 60000,  600000000,8,  50, 90, '{"repair":true,"browser_exploration":true}');

CREATE TABLE organizations (
  id            uuid PRIMARY KEY,
  name          text NOT NULL,
  slug          text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,40}$'),
  plan_id       text NOT NULL REFERENCES plans(id),
  llm_code_context_enabled boolean NOT NULL DEFAULT true,
  suspended_at  timestamptz,
  suspended_reason text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY,
  idp_subject   text NOT NULL UNIQUE,
  email         citext NOT NULL,
  display_name  text,
  mfa_enabled   boolean NOT NULL DEFAULT false,
  disabled_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz
);
CREATE INDEX users_email_idx ON users (email);

CREATE TABLE memberships (
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('owner','admin','engineer','viewer')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships (user_id);

CREATE TABLE invitations (
  id          uuid PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email       citext NOT NULL,
  role        text NOT NULL CHECK (role IN ('admin','engineer','viewer')),
  token_hash  bytea NOT NULL UNIQUE,
  invited_by  uuid NOT NULL REFERENCES users(id),
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name         text NOT NULL,
  key_prefix   text NOT NULL UNIQUE,
  key_hash     bytea NOT NULL,
  scopes       text[] NOT NULL,
  project_ids  uuid[],
  created_by   uuid NOT NULL REFERENCES users(id),
  expires_at   timestamptz NOT NULL,
  last_used_at timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feature_flags (
  key         text NOT NULL,
  org_id      uuid REFERENCES organizations(id) ON DELETE CASCADE,
  enabled     boolean NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (key, org_id)
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
  project_id        uuid NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
  installation_id   uuid NOT NULL REFERENCES github_installations(id) ON DELETE CASCADE,
  github_repo_id    bigint NOT NULL,
  full_name         text NOT NULL,
  default_branch    text NOT NULL,
  detected_stack    jsonb,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE targets (
  id                        uuid PRIMARY KEY,
  org_id                    uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id                uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  base_url                  text NOT NULL CHECK (base_url ~ '^https?://'),
  host                      text NOT NULL,
  environment               text NOT NULL CHECK (environment IN ('sandbox','preview','staging','production')),
  verification_method       text CHECK (verification_method IN ('dns_txt','http_file','platform')),
  verification_token        text NOT NULL,
  status                    text NOT NULL CHECK (status IN ('pending','verified','failed','revoked')),
  verified_at               timestamptz,
  verification_expires_at   timestamptz,
  last_checked_at           timestamptz,
  last_check_detail         jsonb,
  resolved_ips              inet[],
  production_allowed_by     uuid REFERENCES users(id),
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
  ciphertext   bytea NOT NULL,
  wrapped_dek  bytea NOT NULL,
  created_by   uuid NOT NULL REFERENCES users(id),
  rotated_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);

CREATE TABLE ingest_tokens (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name         text NOT NULL,
  token_prefix text NOT NULL UNIQUE,
  token_hash   bytea NOT NULL,
  created_by   uuid NOT NULL REFERENCES users(id),
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
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
  options         jsonb NOT NULL,
  status          text NOT NULL CHECK (status IN ('queued','running','awaiting_review','completed','failed','cancelled','budget_exhausted')),
  progress        jsonb NOT NULL DEFAULT '[]',
  agent_thread_id text UNIQUE,
  stats           jsonb,
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
  spec             jsonb NOT NULL,
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
  id              uuid PRIMARY KEY,
  org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version         int NOT NULL,
  status          text NOT NULL CHECK (status IN ('generating','validating','ready','partially_ready','failed')),
  commit_sha      text,
  artifact_prefix text NOT NULL,
  agent_thread_id text UNIQUE,
  generator_info  jsonb NOT NULL DEFAULT '{}',
  created_by      uuid NOT NULL REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, version)
);

CREATE TABLE test_cases (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  suite_id          uuid NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
  workflow_id       uuid NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  type              text NOT NULL CHECK (type IN ('playwright','k6_scenario','k6_probe')),
  path              text NOT NULL,
  artifact_key      text NOT NULL,
  sha256            bytea NOT NULL,
  validation_status text NOT NULL CHECK (validation_status IN ('pending','valid','invalid','flaky')),
  validation_detail jsonb,
  attempts          int NOT NULL DEFAULT 0 CHECK (attempts <= 4),
  UNIQUE (suite_id, workflow_id, type)
);

CREATE TABLE load_profiles (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name          text NOT NULL,
  type          text NOT NULL CHECK (type IN ('smoke','baseline','load','stress','spike','soak')),
  executor      text NOT NULL CHECK (executor IN ('ramping-vus','ramping-arrival-rate','constant-arrival-rate')),
  stages        jsonb NOT NULL,
  max_vus       int NOT NULL CHECK (max_vus BETWEEN 1 AND 1000),
  duration_s    int NOT NULL CHECK (duration_s BETWEEN 10 AND 3600),
  think_time_min_s numeric NOT NULL DEFAULT 1,
  think_time_max_s numeric NOT NULL DEFAULT 3,
  workload_mix  jsonb NOT NULL,
  thresholds    jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name),
  CHECK (think_time_min_s >= 0 AND think_time_max_s >= think_time_min_s)
);

-- ============ Runs & artifacts ============
CREATE TABLE runs (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  target_id         uuid REFERENCES targets(id),
  suite_id          uuid REFERENCES test_suites(id),
  load_profile_id   uuid REFERENCES load_profiles(id),
  kind              text NOT NULL CHECK (kind IN ('functional','load','probe','verification')),
  environment_kind  text NOT NULL CHECK (environment_kind IN ('target_url','sandbox_build')),
  build_ref         jsonb,
  status            text NOT NULL CHECK (status IN ('pending_approval','queued','provisioning','running','cancelling',
                                                    'analyzing','passed','failed','invalid','cancelled','errored','rejected')),
  status_reason     text,
  requested_by      uuid REFERENCES users(id),
  requested_via     text NOT NULL CHECK (requested_via IN ('ui','api_key','system')),
  api_key_id        uuid REFERENCES api_keys(id),
  idempotency_key   text,
  notes             text,
  config_snapshot   jsonb NOT NULL,
  quota_reservation jsonb NOT NULL,
  summary           jsonb,
  generator_health  jsonb,
  queued_at         timestamptz,
  started_at        timestamptz,
  finished_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
CREATE INDEX runs_project_idx  ON runs (project_id, created_at DESC);
CREATE INDEX runs_active_org   ON runs (org_id)    WHERE status IN ('queued','provisioning','running','cancelling');
CREATE INDEX runs_active_tgt   ON runs (target_id) WHERE status IN ('queued','provisioning','running','cancelling');
CREATE INDEX runs_baseline_idx ON runs (load_profile_id, finished_at DESC) WHERE status = 'passed' AND kind = 'load';
CREATE INDEX runs_queued_idx   ON runs (queued_at) WHERE status = 'queued';

CREATE TABLE run_events (
  run_id      uuid NOT NULL,
  seq         bigint NOT NULL,
  org_id      uuid NOT NULL,
  type        text NOT NULL,
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE run_events_default PARTITION OF run_events DEFAULT;

CREATE TABLE run_metric_summaries (
  run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  org_id       uuid NOT NULL,
  scope        text NOT NULL,
  time_window  text NOT NULL CHECK (time_window IN ('steady_state','full')),
  requests     bigint NOT NULL,
  rps          numeric NOT NULL,
  error_rate   numeric NOT NULL,
  p50_ms numeric, p90_ms numeric, p95_ms numeric, p99_ms numeric, max_ms numeric,
  PRIMARY KEY (run_id, scope, time_window)
);

CREATE TABLE runner_leases (
  id               uuid PRIMARY KEY,
  org_id           uuid NOT NULL,
  job_type         text NOT NULL CHECK (job_type IN ('functional','load','probe','sandbox_build','sandbox_env',
                                                     'coding_session','security_scan','discovery_snapshot','test_validation')),
  subject_id       uuid NOT NULL,
  runner_id        text NOT NULL,
  state            text NOT NULL CHECK (state IN ('active','completed','failed','expired','cancelled')),
  cancel_requested boolean NOT NULL DEFAULT false,
  spec             jsonb NOT NULL,
  expires_at       timestamptz NOT NULL,
  last_heartbeat_at timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz
);
CREATE UNIQUE INDEX runner_leases_one_live ON runner_leases (job_type, subject_id) WHERE state = 'active';
CREATE INDEX runner_leases_expiry ON runner_leases (expires_at) WHERE state = 'active';

-- Jobs waiting for a runner (non-run jobs: sandbox builds, coding sessions, scans, validations)
CREATE TABLE execution_jobs (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  job_type     text NOT NULL,
  subject_id   uuid NOT NULL,
  spec         jsonb NOT NULL,
  status       text NOT NULL CHECK (status IN ('queued','leased','completed','failed','cancelled')),
  result       jsonb,
  queued_at    timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
CREATE INDEX execution_jobs_queued ON execution_jobs (queued_at) WHERE status = 'queued';

CREATE TABLE artifacts (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id            uuid REFERENCES runs(id) ON DELETE CASCADE,
  repair_attempt_id uuid,
  kind              text NOT NULL CHECK (kind IN ('k6_raw','k6_summary','k6_timeseries','playwright_report','playwright_trace',
                                                  'screenshot','video','har','log_bundle','code_snapshot','patch',
                                                  'test_bundle','report_html','report_json','scan_report','openapi_spec',
                                                  'telemetry_snapshot')),
  storage_key       text NOT NULL UNIQUE,
  size_bytes        bigint NOT NULL,
  sha256            bytea NOT NULL,
  content_type      text NOT NULL DEFAULT 'application/octet-stream',
  sensitivity       text NOT NULL CHECK (sensitivity IN ('normal','may_contain_secrets','source_code')),
  expires_at        timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifacts_expiry ON artifacts (expires_at);
CREATE INDEX artifacts_run    ON artifacts (run_id);

-- ============ Findings, evidence, diagnosis ============
CREATE TABLE findings (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id         uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type               text NOT NULL CHECK (type IN ('functional_failure','flaky_test','slo_breach','regression',
                                                   'saturation','error_spike','invariant_violation')),
  severity           text NOT NULL CHECK (severity IN ('critical','high','medium','low')),
  fingerprint        text NOT NULL,
  title              text NOT NULL,
  status             text NOT NULL CHECK (status IN ('open','diagnosing','diagnosed','fixing','fix_proposed','resolved','wont_fix','duplicate')),
  workflow_id        uuid REFERENCES workflows(id) ON DELETE SET NULL,
  first_run_id       uuid NOT NULL REFERENCES runs(id),
  last_run_id        uuid NOT NULL REFERENCES runs(id),
  occurrences        int NOT NULL DEFAULT 1,
  headline           jsonb NOT NULL,
  resolved_by_run_id uuid REFERENCES runs(id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
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
  locator      jsonb NOT NULL,
  summary      text NOT NULL,
  snapshot     jsonb,
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
  next_steps      jsonb,
  requested_by    uuid REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
);
CREATE UNIQUE INDEX diagnoses_one_active ON diagnoses (finding_id)
  WHERE status IN ('queued','collecting','hypothesizing','checking','experimenting');

CREATE TABLE hypotheses (
  id                 uuid PRIMARY KEY,
  org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  diagnosis_id       uuid NOT NULL REFERENCES diagnoses(id) ON DELETE CASCADE,
  rank               int NOT NULL CHECK (rank >= 1),
  category           text NOT NULL CHECK (category IN ('n_plus_one','missing_index','unbounded_query','pool_exhaustion',
                       'race_condition','blocking_io','cpu_bound_handler','memory_leak','missing_cache','config_limit',
                       'external_dependency','logic_error','missing_validation','other')),
  statement          text NOT NULL,
  mechanism          text NOT NULL,
  fix_direction      text,
  code_locations     jsonb NOT NULL,
  confidence         text NOT NULL CHECK (confidence IN ('low','medium','high')),
  confidence_factors jsonb NOT NULL,
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
  claim         text NOT NULL,
  check_result  text NOT NULL CHECK (check_result IN ('verified','unverifiable','failed')),
  PRIMARY KEY (hypothesis_id, evidence_id)
);

-- ============ Repair & verification ============
CREATE TABLE repair_attempts (
  id                    uuid PRIMARY KEY,
  org_id                uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id            uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  finding_id            uuid NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  hypothesis_id         uuid NOT NULL REFERENCES hypotheses(id),
  attempt_no            int NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
  base_commit_sha       text NOT NULL,
  constraints           jsonb NOT NULL,
  status                text NOT NULL CHECK (status IN ('queued','planning','coding','candidate','verifying','verified',
                                                        'verification_failed','awaiting_approval','approved','pr_opened',
                                                        'rejected','abandoned','failed','budget_exhausted')),
  status_reason         text,
  patch_artifact_id     uuid REFERENCES artifacts(id),
  regression_test_paths text[],
  diff_stats            jsonb,
  agent_summary         text,
  agent_thread_id       text UNIQUE,
  token_usage           jsonb,
  requested_by          uuid NOT NULL REFERENCES users(id),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (finding_id, hypothesis_id, attempt_no)
);
CREATE INDEX repair_attempts_finding ON repair_attempts (finding_id, created_at DESC);
ALTER TABLE artifacts ADD CONSTRAINT artifacts_repair_fk FOREIGN KEY (repair_attempt_id) REFERENCES repair_attempts(id) ON DELETE CASCADE;

CREATE TABLE verification_gates (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  repair_attempt_id uuid NOT NULL REFERENCES repair_attempts(id) ON DELETE CASCADE,
  gate              text NOT NULL CHECK (gate IN ('build','existing_tests','fail_to_pass','functional_suite',
                                                  'security','load_comparison','invariant_probe','diff_policy')),
  status            text NOT NULL CHECK (status IN ('pending','running','passed','failed','skipped','errored')),
  applicable        boolean NOT NULL,
  run_ids           uuid[] NOT NULL DEFAULT '{}',
  result            jsonb,
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
  scope             text NOT NULL,
  metric            text NOT NULL CHECK (metric IN ('p50_ms','p95_ms','p99_ms','error_rate','rps','capacity_vus')),
  baseline_value    numeric NOT NULL,
  candidate_value   numeric NOT NULL,
  delta_pct         numeric NOT NULL,
  ci_low_pct        numeric NOT NULL,
  ci_high_pct       numeric NOT NULL,
  noise_floor_pct   numeric NOT NULL,
  verdict           text NOT NULL CHECK (verdict IN ('improved','no_change','regressed','inconclusive')),
  method            text NOT NULL DEFAULT 'bootstrap_bca_10k',
  environment_controlled boolean NOT NULL DEFAULT true,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pull_requests (
  id                uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  repair_attempt_id uuid UNIQUE REFERENCES repair_attempts(id),
  suite_id          uuid UNIQUE REFERENCES test_suites(id),
  repository_id     uuid NOT NULL REFERENCES repositories(id),
  branch            text NOT NULL CHECK (branch ~ '^reliability/'),
  number            int,
  url               text,
  state             text NOT NULL CHECK (state IN ('creating','open','merged','closed','failed')),
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((repair_attempt_id IS NULL) <> (suite_id IS NULL))
);

-- ============ Reports ============
CREATE TABLE reports (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  params        jsonb NOT NULL,
  status        text NOT NULL CHECK (status IN ('queued','ready','failed')),
  html_artifact_id uuid REFERENCES artifacts(id),
  json_artifact_id uuid REFERENCES artifacts(id),
  share_token_hash bytea UNIQUE,
  share_expires_at timestamptz,
  requested_by  uuid NOT NULL REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ============ Governance ============
CREATE TABLE approvals (
  id            uuid PRIMARY KEY,
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  subject_type  text NOT NULL CHECK (subject_type IN ('run','repair_pr','production_target','workflow_destructive','suite_pr')),
  subject_id    uuid NOT NULL,
  summary       jsonb NOT NULL,
  requested_by  uuid REFERENCES users(id),
  decided_by    uuid REFERENCES users(id),
  decision      text CHECK (decision IN ('approved','rejected')),
  reason        text,
  expires_at    timestamptz NOT NULL,
  decided_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (subject_type <> 'production_target' OR decided_by IS NULL OR decided_by <> requested_by)
);
CREATE INDEX approvals_pending ON approvals (org_id) WHERE decision IS NULL;
CREATE UNIQUE INDEX approvals_one_pending ON approvals (subject_type, subject_id) WHERE decision IS NULL;

CREATE TABLE abuse_reviews (
  id           uuid PRIMARY KEY,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  reason       text NOT NULL,
  signals      jsonb NOT NULL,
  status       text NOT NULL CHECK (status IN ('open','cleared','suspended')),
  decided_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  decided_at   timestamptz
);

CREATE TABLE audit_events (
  seq           bigserial,
  org_id        uuid,
  actor_type    text NOT NULL CHECK (actor_type IN ('user','api_key','system','agent','operator','runner')),
  actor_id      text NOT NULL,
  action        text NOT NULL,
  subject_type  text,
  subject_id    text,
  ip            inet,
  user_agent    text,
  request_id    text,
  metadata      jsonb NOT NULL DEFAULT '{}',
  prev_hash     bytea NOT NULL,
  hash          bytea NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (seq, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
CREATE INDEX audit_org_time ON audit_events (org_id, created_at DESC);

CREATE TABLE audit_anchors (
  org_key     text NOT NULL,
  anchor_date date NOT NULL,
  last_seq    bigint NOT NULL,
  last_hash   bytea NOT NULL,
  storage_key text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_key, anchor_date)
);

CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  topic         text NOT NULL,
  payload       jsonb NOT NULL,
  org_id        uuid,
  attempts      int NOT NULL DEFAULT 0,
  available_at  timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  failed_at     timestamptz,
  last_error    text,
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

CREATE TABLE usage_ledger (
  id         uuid NOT NULL,
  org_id     uuid NOT NULL,
  period     date NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('vu_minutes','sandbox_minutes','llm_tokens','runs')),
  quantity   bigint NOT NULL,
  entry_type text NOT NULL CHECK (entry_type IN ('reserve','commit','release','adjust')),
  subject_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE usage_ledger_default PARTITION OF usage_ledger DEFAULT;
CREATE INDEX usage_period ON usage_ledger (org_id, period, kind);

CREATE TABLE llm_usage (
  id            uuid NOT NULL,
  org_id        uuid NOT NULL,
  purpose       text NOT NULL,
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
CREATE TABLE llm_usage_default PARTITION OF llm_usage DEFAULT;
CREATE INDEX llm_usage_org ON llm_usage (org_id, created_at DESC);

CREATE TABLE agent_threads (
  thread_id   text PRIMARY KEY,
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  graph       text NOT NULL CHECK (graph IN ('discovery','testgen','diagnosis','repair')),
  subject_id  uuid NOT NULL,
  pending_interrupt jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
