# Platform observability

Our own SLOs and alerts (operations.md §6). Customer evidence telemetry is separate
(`deploy/otel/collector.yaml`, per-tenant).

| Path | What |
|---|---|
| `rules/platform-slo.rules.yml` | Recording rules (SLIs per window) and alerts: multi-window burn rates for availability, read latency and job start; kill latency; outbox lag; queue age; RLS denials; integrity violations; canary; restore drill; runner lease expiries; LLM degradation |
| `rules/tests/platform-slo.test.yml` | `promtool test rules` unit tests (CI job `observability`) |
| `dashboards/platform-slo.json` | Grafana dashboard: SLO status, burn rates, RED, queues, execution plane, LLM, checks |
| `../otel/platform-collector.yaml` | Collector that scrapes the API and workers and writes to Mimir tenant `platform` |

## Where the numbers come from

* **In-process** (`reliability/platform/metrics.py`): `re_http_requests_total`,
  `re_http_request_duration_seconds` (route templates only), `re_job_start_seconds`
  (queued → first container running for runs; queued → leased for runner jobs),
  `re_kill_latency_seconds` (runner-measured cancel → tool stopped), `re_jobs_total`,
  `re_llm_*`, `re_rls_denials_total`, `re_admission_rejections_total`,
  `re_runner_leases_expired_total`.
* **From Postgres at scrape time** (`reliability/modules/ops/metrics.py`, API only):
  queue depth and oldest age, outbox lag, active runs and leases, heartbeat lag, kill
  switch, and the `platform_checks` table — canary, hourly integrity check, weekly
  restore drill (`re_platform_check_*`, `re_integrity_violations`).

## Running locally

```bash
curl -s localhost:8000/metrics | grep ^re_            # open in local/test; token elsewhere
promtool check rules deploy/observability/rules/platform-slo.rules.yml
promtool test rules deploy/observability/rules/tests/platform-slo.test.yml
```

Import the dashboard in the local Grafana (`deploy/local`, port 3001) with a Prometheus
data source pointing at the local Mimir.

## Paging

`severity: page` → on-call; `severity: ticket` → platform queue; `severity: info` → chat.
Job-start alerts carry `page_hours: business` (NFR-4 pages during business hours only).
Every page links a runbook in `docs/runbooks/`.
