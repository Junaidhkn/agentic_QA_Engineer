# Abuse review queue

**What lands here** (`modules/abuse/service.py`, threat model A1). Load runs against a
customer target are refused with `UNDER_REVIEW` and the org is queued when either rule fires:

| Rule | Fires when |
|---|---|
| `new_org_max_load_new_target` | org younger than 7 days **and** ≥ 80 % of its plan's max VUs (at least 50) **and** the target has never completed a load test |
| `verification_churn` | ≥ 5 targets created in 24 h with ≥ 3 failed verifications (hunting for hosts it can verify) |

Smaller load tests, functional runs and sandbox runs keep working while a review is open.
Owners are emailed when the review opens and when it is decided. **Target:** decided within 4
business hours (`AbuseReviewWaiting` tickets after that).

## Working the queue

```bash
reliability abuse list                 # open reviews, oldest first
reliability abuse show <review-id>     # signals: org age, VUs vs plan, host, churn counts
```

Check, in order:
1. **Who is it?** Org name/slug and owner email domain vs the target host (`SELECT name, slug
   FROM organizations WHERE id = …`; owners via `memberships` + `users`). A company domain
   matching the host is the common, benign case.
2. **The host.** Does the target belong to them — company site, `staging.`/`preview.`
   subdomain of their domain, a PaaS preview URL in their account? How was it verified
   (`targets.verification_method`, `last_check_detail`)? Shared hosting that slipped past
   verification (threat model A2) is a reason to suspend and file a bug.
3. **Churn reviews.** List the org's targets from the last day: many unrelated domains with
   failed verifications = probing.
4. **When unsure**, email the owner asking what they are testing; leave the review open.

Decide (the note is audited and kept with the review):

```bash
reliability abuse clear <review-id> --note "acme.com staff, staging host verified by DNS"
reliability abuse suspend <review-id> --note "targets unrelated third-party sites"
```

`clear` lets the org's held runs be re-submitted and the org is not queued again.
`suspend` suspends the whole org (admissions refused, active leases cancelled) — follow
[abuse-report-egress-ip.md](abuse-report-egress-ip.md) if traffic already reached a third party.
