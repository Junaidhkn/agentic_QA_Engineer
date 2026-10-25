# Zero-code OpenTelemetry instrumentation layer for sandboxed apps (FR-23, ADR-0011).
# Sandbox app images copy /otel/{python,node} from this image (see runner_agent/sandbox_env/templates):
#   Python: PYTHONPATH=/otel/python/opentelemetry/instrumentation/auto_instrumentation:/otel/python
#           (sitecustomize from opentelemetry-instrumentation; same mechanism as the OTel operator)
#   Node:   NODE_OPTIONS=--require /otel/node/node_modules/@opentelemetry/auto-instrumentations-node/build/src/register.js
# Versions are pinned; bump together and rebuild via runners/images/README.md.
FROM python:3.12-slim AS python
RUN pip install --no-cache-dir --target /otel/python \
      opentelemetry-distro==0.50b0 \
      opentelemetry-exporter-otlp-proto-http==1.29.0 \
    && PYTHONPATH=/otel/python python -m opentelemetry.instrumentation.bootstrap -a requirements \
       | pip install --no-cache-dir --target /otel/python -r /dev/stdin

FROM node:22-slim AS node
WORKDIR /otel/node
RUN npm init -y >/dev/null \
    && npm install --omit=dev --no-audit --no-fund \
       @opentelemetry/auto-instrumentations-node@0.55.0 @opentelemetry/api@1.9.0

FROM busybox:1.37
COPY --from=python /otel/python /otel/python
COPY --from=node /otel/node /otel/node
# Exporter defaults; the sandbox sets OTEL_EXPORTER_OTLP_ENDPOINT to its collector.
ENV OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf OTEL_NODE_RESOURCE_DETECTORS=env,host
