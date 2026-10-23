# Runner images

Images the runner agent starts inside gVisor sandboxes (ADR-0009). All are pinned by tag in
config and **by digest in production** (`RA_K6_IMAGE=grafana/k6@sha256:…`), signed with cosign in
CI, and pre-pulled into the runner AMI by Packer.

| Image | Used for | Notes |
|---|---|---|
| `k6/` | load, probe, API smoke, validation | upstream k6, non-root |
| `playwright/` | functional browser tests, validation | Playwright + Chromium; `package.json` here is also used by the process driver locally |
| `runner-agent/` | the agent itself on execution hosts | talks to the docker daemon on the host |
| Smokescreen (upstream) | per-run egress proxy | allowlist ACL generated per job |

Local development without Docker: `cd runners/images/playwright && npm ci && npx playwright install chromium`,
then point `RA_PLAYWRIGHT_DIR` here and `RA_K6_BINARY` at a local k6.
