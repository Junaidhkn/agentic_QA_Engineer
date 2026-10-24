# CI integration (FR-32)

`reliability-check.sh` starts a run with a scoped API key, polls `GET /v1/runs/{id}/verdict`
and maps the verdict to an exit code a pipeline can gate on:

| Exit | Meaning |
|---|---|
| 0 | `passed` |
| 1 | `failed` — thresholds, functional failures (see `failed_thresholds`, `new_findings`) |
| 2 | could not run: invalid, cancelled, errored, timeout, bad configuration |
| 3 | waiting for approval (first load run against a target, production targets) |

The verdict response (`run_id, status, finished, passed, reason, failed_thresholds, summary,
new_findings, url`) is a published contract: fields are only added within `/v1`.

Runs are idempotent per CI attempt (`Idempotency-Key: ci-<run id>-<attempt>-<kind>`), so a
retried job never sends a second load test. Targets must be verified first (UF-2); the API
key needs `runs:write` and `runs:read`. `reliability-check.yml` is a GitHub Actions example that
gates on a successful staging deployment: functional suite first, then a short load profile.
