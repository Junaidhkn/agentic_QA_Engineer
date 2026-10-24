# Sandbox builds (ADR-0011)

Sandbox images are built by the runner host's Docker daemon through a **per-job BuildKit
builder** (`docker buildx create --driver docker-container`) attached to an internal build
network whose only egress is a Smokescreen proxy allowing the package mirror hosts
(`RA_MIRROR_HOSTS`; CodeArtifact in AWS — see `infra/terraform/modules/package-mirror`).

| Strategy | When | How |
|---|---|---|
| `compose` | repository has `docker-compose.yml` / `compose.yaml` | compose file hardened by `runner_agent.sandbox_env.compose.rewrite_compose` (gVisor, no published ports, no privileged/host namespaces/host mounts, limits) and built with `docker compose build` |
| `dockerfile` | repository `Dockerfile` | `docker buildx build` with the repository Dockerfile |
| `template` | otherwise (Node: Next.js/Express; Python: FastAPI/Flask/Django) | Dockerfile rendered from `services/runner-agent/src/runner_agent/sandbox_env/templates/` with exec-form `RUN`/`CMD` lines from the build plan |

Layer caches are per project on the host (`RA_BUILD_CACHE_DIR/<project_id>`, BuildKit
`type=local`). Template images copy the zero-code instrumentation layer from
`runners/images/otel-autoinstrument`. Build plans are computed by the control plane
(`reliability.modules.environments.plan`) and shown to users before anything runs.
