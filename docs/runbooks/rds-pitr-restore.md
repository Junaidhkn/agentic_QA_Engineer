# RDS: failover, point-in-time restore, failed restore drill

RPO ≤ 5 min (PITR, 14 days), RTO ≤ 1 h (operations §7). Backups are also replicated to the DR
region (`aws_db_instance_automated_backups_replication`).

## Primary failure (automatic)

Multi-AZ fails over in ~1–2 min; the API answers 503 meanwhile (`/readyz` fails), workers
retry. `aws_db_event_subscription` pages on `failover`. Nothing to do unless it does not
recover in 10 min — then open an AWS support case and continue below.

## Data corruption or a bad migration → PITR

1. **Stop writers.** Scale every ECS service except `web` to 0 (or set the global kill
   switch first if runs are active, `kill-switch.md`). Note the time `T` just before the
   corruption (from audit events, deploy time or the migration log).
2. **Restore** to a new instance in the same subnet group and security group:
   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier <name>-pg --target-db-instance-identifier <name>-pg-pitr \
     --restore-time <T in ISO-8601 UTC> --db-subnet-group-name <name>-data \
     --vpc-security-group-ids <db sg> --db-parameter-group-name <name>-pg16 --multi-az
   aws rds wait db-instance-available --db-instance-identifier <name>-pg-pitr
   ```
3. **Verify** the copy before cutting over (one-off task with the URLs pointed at the copy):
   ```bash
   RE_DATABASE_SYSTEM_URL=…pitr… reliability integrity-check
   ```
   Exit 0 = invariants and audit chains verify. Spot-check the data that was corrupted.
4. **Cut over**: update the `database-url`, `database-system-url` and
   `database-migration-url` secrets to the new endpoint, then force new deployments of all
   services (they read secrets at start). Bring services back to their normal counts.
5. **Rename** later in a maintenance window if you want the original identifier back, and
   import the new instance into Terraform state (`terraform import`), or adopt the PITR
   identifier as the module's instance.
6. Writes between `T` and the restore are lost; list affected orgs from the old instance
   (`runs`, `findings`, `repair_attempts` with `created_at > T`) and tell them.

## Region loss

Restore the latest replicated backup in the DR region (`aws rds
restore-db-instance-to-point-in-time --source-db-instance-automated-backups-arn …`), then
rebuild the stack there from Terraform (≤ 4 h, multi-region is Deferred).

## `RestoreDrillFailed` / `RestoreDrillStale`

The weekly drill (`reliability restore-drill --mode rds`, EventBridge schedule
`<name>-restore-drill`) restores the newest automated snapshot into a temporary instance,
checks row counts, migration revision and every integrity invariant, then deletes it.

1. `SELECT status, detail FROM platform_checks WHERE name = 'restore_drill'` — `error` says
   which step failed (no snapshot, restore timeout, counts, revision, invariants).
2. Snapshot or restore errors → check the drill task's log group `/ecs/<name>/restore-drill`
   and IAM (the drill role may only touch `<name>-drill-*` instances).
3. Count/invariant failures on the *copy* may mean the backups are bad: treat as an incident,
   take a manual snapshot now, and investigate before the bad state ages out of PITR.
4. Make sure no `<name>-drill-*` instance is left behind (`aws rds describe-db-instances`).
5. Re-run by hand: `aws ecs run-task` with the `<name>-restore-drill` task definition.

Local equivalent (CI-tested): `reliability restore-drill --mode dump --source-url … --scratch-url …`.
