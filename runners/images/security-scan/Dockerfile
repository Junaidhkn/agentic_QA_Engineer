# Runner agent + security scanners for the verification `security` gate (low-level-design §9).
# The agent runs these on the extracted source of the base and patched builds (they never execute
# the customer's code) and the control plane compares the findings. Pin versions; bump together.
FROM golang:1.23-bookworm AS go
RUN GOBIN=/out go install github.com/zricethezav/gitleaks/v8@v8.21.2 \
 && GOBIN=/out go install github.com/google/osv-scanner/cmd/osv-scanner@v1.9.1

FROM ghcr.io/reliability-engineer/runner-agent:latest
USER root
RUN /app/.venv/bin/python -m ensurepip >/dev/null 2>&1 || true \
 && uv pip install --python /app/.venv/bin/python "semgrep==1.97.0"
COPY --from=go /out/gitleaks /out/osv-scanner /usr/local/bin/
# Offline-capable semgrep rules: mirror p/default into the image at build time and point the agent at it.
RUN mkdir -p /opt/semgrep && /app/.venv/bin/semgrep --config p/default --dump-config > /opt/semgrep/rules.yml || true
ENV RA_SEMGREP_CONFIG=/opt/semgrep/rules.yml
