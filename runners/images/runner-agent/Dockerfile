FROM python:3.12.8-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/runner-agent/pyproject.toml services/runner-agent/uv.lock ./
COPY services/runner-agent/src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" RA_DRIVER=docker RA_ENV=production
CMD ["runner-agent"]
