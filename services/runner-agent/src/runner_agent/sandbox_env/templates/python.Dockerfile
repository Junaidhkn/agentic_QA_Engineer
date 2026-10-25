# Reliability Engineer sandbox template: Python (FastAPI/ASGI, Flask, Django).
# Rendered by runner_agent.sandbox_env.compose.render_dockerfile — {{INSTALL}}, {{BUILD}}, {{START}} are
# JSON-form RUN/CMD lines produced from the build plan (never shell strings).
ARG OTEL_IMAGE
FROM ${OTEL_IMAGE} AS otel
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY . .
{{INSTALL}}
# Repair checks run the regression test with pytest inside this image (agent-workflows §6).
RUN python -c "import pytest" 2>/dev/null || pip install --no-cache-dir "pytest>=8,<9"
{{BUILD}}
COPY --from=otel /otel/python /otel/python
# Zero-code instrumentation without changing the start command (same mechanism as the OTel operator).
ENV PYTHONPATH=/otel/python/opentelemetry/instrumentation/auto_instrumentation:/otel/python
RUN useradd --uid 10001 --create-home app && chown -R app /app
USER 10001
{{START}}
