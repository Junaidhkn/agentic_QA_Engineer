# Kill switch

**Use when** test traffic must stop immediately: an abuse report, a run hammering the wrong
host, a `KillLatencyBreach` page, a runner bug generating unexpected traffic.

**Effect** (`platform/flags.py`, `runners/service.py`, `admission/service.py`):
* admission refuses every new run with `503 UNAVAILABLE`, `Retry-After: 600`;
* every active lease gets `cancel_requested`; runners stop the test tool within one heartbeat
  (3 s) plus their stop latency (`re_kill_latency_seconds`, NFR-7: < 10 s);
* queued runs error at lease time ("execution paused by operator").

## Steps

```bash
reliability kill-switch on                 # global
reliability kill-switch on --org <org-id>  # one organization
```

The command prints how many leases were told to cancel. Then:

1. Watch `re_runner_leases_active` fall to 0 on the dashboard (≤ 15 s). Leases whose host
   stopped answering expire after 15 s and their runs error as "runner lost".
2. If traffic still reaches a target after 30 s, the runner host is not obeying: terminate
   the instance(s) from the execution-plane ASG (`runner-fleet-replacement.md`, emergency).
3. Post the reason in the incident channel; `KillSwitchActive` (info) shows it is on.

## Turning it off

```bash
reliability kill-switch off [--org <org-id>]
```

Runs refused while it was on are not resubmitted; tell affected customers (status page) so
they re-run. Cancelled runs show `cancelled: …` in the UI.

## Verify

* `curl -s -H "Authorization: Bearer $RE_METRICS_TOKEN" https://<api task>:8000/metrics | grep re_kill_switch_active`
* Chaos test: `tests/integration/test_phase10_chaos.py::test_runner_loss_and_kill_switch`.
