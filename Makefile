CP := services/control-plane
RA := services/runner-agent

.PHONY: services migrate api worker relay web test test-cp test-runner test-web lint openapi

services:            ## start Postgres + Redis without Docker and bootstrap roles
	./scripts/dev-services.sh

migrate:
	cd $(CP) && uv run alembic upgrade head

api:
	cd $(CP) && uv run uvicorn reliability.main_api:app --reload --port 8000

worker:
	cd $(CP) && uv run celery -A reliability.main_worker:celery_app worker -Q default,agents,github -c 4

relay:
	cd $(CP) && uv run reliability outbox-relay

web:
	pnpm --filter @re/web dev

openapi:
	cd $(CP) && uv run reliability export-openapi && cd ../.. && pnpm --filter @re/api-client generate

test: test-cp test-runner test-web

test-cp:
	cd $(CP) && uv run pytest -q

test-runner:
	cd $(RA) && uv run pytest -q

test-web:
	pnpm --filter @re/web test

lint:
	cd $(CP) && uv run ruff check src tests && uv run mypy src && uv run lint-imports --config ../../.importlinter
	pnpm --filter @re/web lint
