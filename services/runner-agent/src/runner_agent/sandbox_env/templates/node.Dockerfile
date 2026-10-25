# Reliability Engineer sandbox template: Node (Next.js, Express).
ARG OTEL_IMAGE
FROM ${OTEL_IMAGE} AS otel
FROM node:22-slim
WORKDIR /app
COPY . .
{{INSTALL}}
{{BUILD}}
COPY --from=otel /otel/node /otel/node
ENV NODE_OPTIONS="--require /otel/node/node_modules/@opentelemetry/auto-instrumentations-node/build/src/register.js"
RUN chown -R node /app
USER node
{{START}}
