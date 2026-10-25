# Launch checklist (Phase 11 → M4 public MVP)

Every box needs an owner and evidence (a link). "Done" means verified on production
infrastructure, not merged code. Items marked **(code ready)** have the software and tests
in this repository; what remains is running them for real.

## 1. Beta exit: one month meeting the SLOs (NFR-3, NFR-4)

- [ ] 30 consecutive days of beta traffic on production infrastructure.
- [ ] `benchmarks/harness/slo_report.py --days 30` exits 0 **(code ready)**: availability
      ≥ 99.9 %, reads < 300 ms ≥ 95 %, jobs started < 30 s ≥ 95 %, no kill > 10 s, canary
      passing ≥ 99 % of samples, no integrity violations, restore drill ≤ 8 days old, zero
      RLS denials. Attach `benchmarks/out/slo-report.md`.
- [ ] Platform load test at reference scale on staging (`benchmarks/harness/platform_load.py`,
      25 lanes, 100 functional/hour, 60 min) passes **(code ready)**.
- [ ] NFR-1 reference workload (1,000 VUs × 5 runs) meets evaluation §2 acceptance.
- [ ] Error budget policy agreed: feature freeze when the 30-day availability budget is spent.

## 2. Security (Phase 10 exit, carried)

- [ ] External penetration test done (`docs/security/pentest-scope.md`); every high fixed and
      retested; mediums triaged with dates.
- [ ] Chaos game day run on staging (`docs/runbooks/game-day.md`), findings closed.
- [ ] Secrets rotated from any value used during beta; GitHub App key rotated
      (`docs/runbooks/github-app-key-rotation.md`).
- [ ] Dependency and image scans clean of criticals (CI `security` job, Trivy on images).

## 3. Legal and compliance (NFR-14)

- [ ] Counsel reviewed and approved `terms`, `aup`, `dpa`, `subprocessors`
      (`services/control-plane/src/reliability/modules/legal/documents/`); remove the "Draft"
      banners and bump versions — every org then re-accepts before its next run **(code ready)**.
- [ ] Zero/limited-retention terms signed with the LLM provider (vendors.md).
- [ ] DPA signature flow for customers who need a countersigned copy (accepting in-product is
      recorded with user, time, IP and user agent).
- [ ] Abuse contact address published next to the egress IP list; mailbox monitored.
- [ ] Data deletion workflow exercised once on production with a test org
      (`docs/runbooks/customer-data-deletion.md`) **(code ready)**.

## 4. Abuse and safety (threat model A1)

- [ ] Abuse review queue staffed: an operator rota with the 4-business-hour target
      (`docs/runbooks/abuse-review-queue.md`); `AbuseReviewWaiting` routed to the rota **(code ready)**.
- [ ] Operators trained on `reliability abuse`, `org suspend`, `kill-switch` and the runbooks.
- [ ] Kill switch drill done on production (on → leases cancelled → off) within the first week.

## 5. Plans and billing

- [ ] Plan limits and prices confirmed by the business owner (`GET /v1/plans`, migration
      `0007_launch`) **(code ready)**. Prices in the migration are proposals.
- [ ] Manual plan changes documented for sales (`reliability org plan <org> --plan <id>`);
      self-serve billing (Stripe) remains post-MVP.
- [ ] Free plan abuse limits reviewed after beta data (VUs, quotas, no repair/exploration).

## 6. Operations

- [ ] On-call rota and paging (PagerDuty) wired to `severity: page`; runbooks linked from every page.
- [ ] Status page live, linked from the dashboard and the Terms.
- [ ] Synthetic canary configured in production (`RE_CANARY_PROJECT_ID/TARGET_ID`) and green.
- [ ] Weekly restore drill has passed at least twice in production.
- [ ] Backups replicated to the DR region verified (restore one copy there once).
- [ ] Support inbox and response targets published.

## 7. Product readiness

- [ ] Design partners completed UF-1 … UF-7 unaided (M3 criterion) and their feedback triaged.
- [ ] Dashboard WCAG 2.1 AA check on the main flows (NFR-15).
- [ ] Docs site: getting started, target verification, CI integration (`examples/ci`), limits.

## Go / no-go

Launch when sections 1–4 are complete; 5–7 may carry dated follow-ups agreed in the go/no-go
meeting. Record the decision and attendees in the launch issue.
