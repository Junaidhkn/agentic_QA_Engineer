# Customer data deletion request

**Use when** an organization owner asks for their data to be deleted (DPA), or an account is
closed. Confirm the request comes from an owner of the org (support ticket from the owner's
verified email) and record the ticket id.

## What is deleted (`modules/ops/orgs.py`)

* every row carrying the org's `org_id` (projects, targets, secrets, workflows, suites,
  runs and events, findings, diagnoses, repairs, reports, usage ledger, API keys, …);
* objects under `org/<org-id>/` in the artifacts bucket (noncurrent versions expire after
  7 days by lifecycle rule);
* LangGraph checkpoints of the org's agent threads (`agent_state` schema);
* users whose only membership was this org.

**Kept, by design:** the org's audit events (append-only for every runtime role, ADR-0023;
retained for the audit retention period and stated in the DPA), daily audit anchors (hashes
only, Object Lock), and one `org.deleted` event in the platform chain with row counts and no
personal data. Customer telemetry in Mimir/Tempo/Loki expires within 30 days (retention);
delete the tenant earlier with each component's tenant-deletion API if the request asks.

## Steps

1. Dry run — prints the slug and what would be deleted:
   ```bash
   reliability org delete <org-id>
   ```
2. Check the counts look right for the customer; uninstall prompts: ask the customer to
   uninstall the GitHub App (we cannot do it for them) and revoke any tokens they created.
3. Delete (irreversible; suspends the org first, then deletes):
   ```bash
   reliability org delete <org-id> --execute --confirm-slug <slug> --operator <your name>
   ```
4. Reply to the ticket with the output's summary (row counts by table, objects removed) and
   the retention statement for audit events and backups: database backups age out of PITR
   after 14 days and are never restored into production for one tenant.

Tested end to end in `tests/integration/test_phase10_orgs.py`.
