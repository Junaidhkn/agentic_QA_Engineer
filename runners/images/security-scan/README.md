# security-scan

The runner-agent image plus **semgrep**, **gitleaks** and **osv-scanner**. Runners that lease
`repair_check` jobs use it so the verification pipeline's `security` gate can compare base and
patched sources (`runner_agent/scan.py`). Only *new* high/critical findings fail the gate; the
control plane also scans the lines a patch adds for secrets and dangerous calls
(`modules/verification/security.py`), so the gate still works on runners without these tools —
the gate result lists which scanners ran and which were missing.

ZAP passive baseline (DAST) is not part of this image yet; it lands with the browser-exploration
runner work (plan.md deviations, Phase 8).
