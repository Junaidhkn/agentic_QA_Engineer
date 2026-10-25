# Chaos game day (quarterly, staging)

Each scenario has an expected platform behaviour (operations.md §3) and the signal that
proves it. The smaller versions run in CI (`tests/integration/test_phase10_chaos.py`,
`test_phase3_e2e.py` stop test); game day repeats them at reference load on the real
topology, with people following the runbooks.

**Before:** announce in the incident channel; start `benchmarks/harness/platform_load.py`
against staging (5–10 lanes) so every scenario happens under load; open the Platform SLOs
dashboard. **After each scenario:** record detection time, recovery time, alerts that fired
(and ones that should have), runbook gaps.

| # | Inject | Expected | Proof |
|---|---|---|---|
| 1 | Reboot RDS with failover (`aws rds reboot-db-instance --force-failover`) | ≤ 2 min of 503 + Retry-After; workers retry; no run lost; `failover` event pages | `/readyz`, RDS event, runs complete |
| 2 | Delete the ElastiCache primary node | Run/agent creation refused (fail closed), reads keep working, live charts pause; recovery after failover; outbox drains | `re_outbox_oldest_age_seconds` spike then 0 |
| 3 | Stop the `outbox-relay` service for 5 min | `OutboxLag` pages after 2 min; no job starts; restart drains, no duplicates | alert, `re_jobs_total` |
| 4 | Terminate 2 runner hosts mid-run (`aws ec2 terminate-instances`, bypassing the drain) | Their runs end `errored: runner lost`, quota refunded, never retried; ASG replaces hosts; `RunnerLeasesExpiring` tickets | runs, usage ledger |
| 5 | Instance refresh of the runner ASG | No run cut short (drain hook), job start p95 stays < 30 s | `re_job_start_seconds`, runs passed |
| 6 | `reliability kill-switch on` during a 1,000-VU run | Last request at the target ≤ 10 s after the switch; `re_kill_latency_seconds` < 10 | target access log, metric |
| 7 | Block the LLM provider (security group egress deny on worker-agents) | Diagnoses "delayed — retrying"; `LlmProviderDegraded` after 15 min; baselines via `llm_disabled` flags work | alerts, diagnosis status |
| 8 | Stop Mimir for 30 min | Collector queues to disk and backfills on recovery; CloudWatch alarms still page; SLO alerts resume | `otelcol_exporter_queue_size` |
| 9 | Deploy an image that returns 500 on `/v1/projects` | ALB 5xx alarm → ECS rolls back within 10 min | deployment events |
| 10 | Restore drill by hand (`aws ecs run-task … restore-drill`) | `restore_drill` ok, timings recorded (RTO evidence) | `platform_checks` |

Write up: one page per game day in the incident tracker, action items with owners.
