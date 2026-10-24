# Abuse report against an egress IP

**Use when** a third party reports load or attack traffic from one of our published egress IPs
(NAT Elastic IPs, `RE_EGRESS_IPS`) or a provider forwards an abuse ticket.

Every request we send targets a host whose owner proved control (DNS TXT or well-known file),
re-checked at admission and hourly (threat model A1). A report means one of: a customer
verified a host they don't own any more, verified a host that forwards to someone else, or a
bug. Treat it as real until shown otherwise.

## Steps

1. **Stop the traffic** if it is ongoing. Find the org from the reported host and time:
   ```sql
   SELECT r.id, r.org_id, r.status, t.host, r.started_at, r.finished_at
   FROM runs r JOIN targets t ON t.id = r.target_id
   WHERE t.host = '<reported host>' AND r.started_at > now() - interval '2 days'
   ORDER BY r.started_at DESC;
   ```
   No match on the host → match on resolved IPs (`targets.resolved_ips`) or apply the
   global kill switch while you look (`kill-switch.md`).
2. **Suspend the org** (stops admissions and cancels its leases, audited):
   ```bash
   reliability org suspend <org-id> --reason "abuse report <ticket>"
   ```
3. **Revoke the target** (heartbeats then cancel any run still against it):
   `UPDATE targets SET status = 'revoked', updated_at = now() WHERE id = '<target-id>';`
4. **Collect evidence** for the reporter and the customer: run ids, timestamps, VU counts and
   request rates (`runs.summary`), the target's verification record (`verification_method`,
   `verified_at`, `last_check_detail`) and the audit trail (`audit_events` where `subject_id`
   is the target or runs).
5. **Reply** to the reporter within 24 h: what happened, that traffic stopped, contact for
   follow-up. Never share customer identity without legal review.
6. **Decide** with the customer: re-verification of the host by its real owner, or closure.
   Unsuspend with `reliability org unsuspend <org-id>` only after that.

## Follow-up

* If verification was bypassed (a bug), it is a security incident: open one, and treat
  every target verified by the same method in the window as suspect.
* Check IP reputation of the egress IPs; if one is blocklisted, rotate it by replacing the
  NAT Elastic IP in `infra/terraform/modules/network` and publish the new list.
