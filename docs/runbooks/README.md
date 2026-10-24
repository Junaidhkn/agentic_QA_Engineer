# Runbooks

Operator procedures for the production platform (operations.md §9). Every page in
`deploy/observability/rules/platform-slo.rules.yml` links one of these.

Commands assume a shell in an ECS task of the current release (`aws ecs execute-command` is
disabled on services; start a one-off task from the `migrate` task definition with the
command overridden), or a workstation with the production secrets loaded. `reliability` is
the operator CLI (`services/control-plane/src/reliability/cli.py`); everything it does is
audited as `operator`.

| Runbook | Use when |
|---|---|
| [kill-switch.md](kill-switch.md) | Test traffic must stop now (abuse, runaway run, kill-latency page) |
| [abuse-report-egress-ip.md](abuse-report-egress-ip.md) | Someone reports attack traffic from our egress IPs |
| [cross-tenant-access-alarm.md](cross-tenant-access-alarm.md) | `RlsDenial` or `IntegrityViolation` fired |
| [rds-pitr-restore.md](rds-pitr-restore.md) | Data loss/corruption, failed restore drill, DB unavailable |
| [runner-fleet-replacement.md](runner-fleet-replacement.md) | gVisor/kernel CVE, bad AMI, hosts dying, job-start SLO burning |
| [llm-provider-degradation.md](llm-provider-degradation.md) | `LlmProviderDegraded`, model outage or quality incident |
| [lgtm-backfill.md](lgtm-backfill.md) | Mimir/Tempo/Loki outage, missing evidence telemetry |
| [github-app-key-rotation.md](github-app-key-rotation.md) | Scheduled rotation or suspected key leak |
| [customer-data-deletion.md](customer-data-deletion.md) | Verified deletion request (DPA) or closed account |

## Triage

1. Open the **Platform SLOs** dashboard (`deploy/observability/dashboards/platform-slo.json`).
2. Availability burn: check the ALB 5xx alarm and the last deploy. A deploy within 10 min
   rolls back automatically on that alarm; if it did not, redeploy the previous image tag
   (`terraform apply -var image_tag=<previous>` in `infra/terraform/envs/<env>-control-plane`).
3. `MetricsDatabaseUnavailable` or `/readyz` failing on Postgres → [rds-pitr-restore.md](rds-pitr-restore.md)
   (failover section). Redis down → runs and agent jobs are refused (fail closed) until
   ElastiCache fails over; reads keep working. Nothing to restore — Redis holds no
   authoritative data.
4. Post in the incident channel: what is affected, what you are doing, next update time.

## Outbox relay

`OutboxLag` means rows in `outbox` are not being published to Celery, so no new job starts.
The relay is the `outbox-relay` ECS service; it is idempotent, so restarting is safe.

1. ECS console → `outbox-relay`: is a task running? Look at its log group
   `/ecs/<name>/outbox-relay` for Redis/Postgres connection errors.
2. Force a new deployment of the service. Lag should fall within a minute.
3. `OutboxDeadLetters`: rows that exhausted retries keep `last_error`. Inspect with
   `SELECT topic, last_error, count(*) FROM outbox WHERE failed_at > now() - interval '1 day' GROUP BY 1, 2`.
   Fix the cause, then re-queue: `UPDATE outbox SET failed_at = NULL, attempts = 0, available_at = now() WHERE id = ANY(...)`.

## Canary

`CanaryFailing`/`CanaryStale`: the synthetic canary org's functional + small load run against
the reference app failed or has not passed for an hour. `SELECT detail FROM platform_checks
WHERE name = 'canary'` names the runs; open them in the UI as the canary org.

* Runs `errored: runner lost` or stuck `queued` → execution plane: [runner-fleet-replacement.md](runner-fleet-replacement.md).
* Runs `failed` on thresholds → is the reference app (staging target) healthy? The canary
  measures our path, so a sick reference app is a false alarm: fix it, then wait 15 min.
* `could not start: TARGET_NOT_VERIFIED` → re-verify the canary target.
* `reliability canary` runs one firing by hand.

## First deploy of an environment

1. `terraform apply` in `envs/<env>-control-plane`, then in `envs/<env>` with
   `control_plane_endpoint_service` = the `runner_endpoint_service_name` output.
2. Write every secret under `<name>/` in Secrets Manager (list: `app_secret_arns` output);
   database URLs use the roles from `scripts/db-bootstrap.sql`, run once as `re_master`
   (password: `db_master_secret_arn`).
3. Run the migration task (`migrate` output), then deploy services.
4. Create the canary org/project/target, set `canary` in the env's tfvars, apply.
5. Run `reliability restore-drill --mode rds …` once by hand (the schedule runs weekly).
