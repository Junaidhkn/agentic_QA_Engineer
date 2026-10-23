# Reliability Engineer

An agentic SaaS that helps teams shipping AI-generated ("vibe-coded") web apps find
scalability bottlenecks and functional failures **before deployment**: it discovers app
workflows, generates Playwright and k6 tests, runs controlled load (up to 1,000 virtual
users) against hosts you prove you own, diagnoses root causes with cited evidence, and
proposes fixes that are independently verified before a human opens them as pull requests.

It never claims complete coverage or guaranteed fixes, never merges, and never deploys.

## Repository layout

| Path | What |
|---|---|
| `docs/architecture/` | System design, ADRs, threat model, evaluation, implementation plan — **start here** |
| `services/control-plane/` | FastAPI API, Celery workers, LangGraph agents, LLM gateway (Python package `reliability`) |
| `services/runner-agent/` | Execution-plane runner: leases jobs, runs k6/Playwright/sandboxes, enforces limits |
| `apps/web/` | Next.js + TypeScript dashboard |
| `packages/api-client/` | TypeScript types generated from the API's OpenAPI document |
| `packages/workflow-ir/` | JSON Schema for the Workflow IR shared by Python and TypeScript |
| `runners/images/` | Container images used by the runner (k6, Playwright, build, scanners) |
| `benchmarks/` | Seeded-defect reference apps and the evaluation harness |
| `infra/` | Terraform (AWS) and Packer (runner hosts) |
| `deploy/local/` | docker-compose stack for local development |

## Quick start (local)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22 + pnpm 10, and either
Docker or local Postgres 16 + Redis 7.

```bash
cp .env.example .env
make services          # or: docker compose -f deploy/local/docker-compose.yml up -d postgres redis
cd services/control-plane && uv sync --extra dev && uv run alembic upgrade head && cd ../..
pnpm install
make api               # http://localhost:8000/docs
make worker            # second terminal
make relay             # third terminal (outbox → Celery)
make web               # http://localhost:3000 — sign in with any email in dev mode
```

Without API credentials the platform runs with `RE_LLM_PROVIDER=fake`, dev sign-in, local
object storage and the local KMS key. Add real credentials in `.env` when ready.

## Tests

```bash
make test              # control plane (needs Postgres + Redis), runner agent, web
make lint
```

## Safety model in one paragraph

Load is only sent to hosts verified by DNS TXT record or a well-known file, through an egress
allowlist; production targets allow only approved smoke runs. Untrusted code runs only in the
execution plane under gVisor with no platform credentials. AI agents propose; deterministic
code executes runs, computes findings and confidence, and decides whether a patch is
verified. PRs are opened only after a person approves. See
`docs/architecture/threat-model.md`.
