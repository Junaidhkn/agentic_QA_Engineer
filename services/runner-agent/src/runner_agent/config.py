"""Runner configuration (prefix ``RA_``; documented in the repository .env.example)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RA_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "staging", "production"] = "local"
    api_url: str = "http://localhost:8000"
    runner_id: str = "local-runner-1"
    runner_token: str = ""
    driver: Literal["process", "docker"] = "process"
    docker_runtime: str = "runsc"
    docker_binary: str = "docker"
    k6_image: str = "grafana/k6:0.54.0"
    playwright_image: str = "mcr.microsoft.com/playwright:v1.49.1-noble"
    egress_proxy_image: str = "ghcr.io/stripe/smokescreen:v0.0.4"
    slots: int = 4
    workdir: str = ".local/runner"
    k6_binary: str = "k6"
    playwright_dir: str = "runners/images/playwright"
    job_types: list[str] = [
        "functional",
        "load",
        "probe",
        "verification",
        "test_validation",
        "sandbox_build",
        "sandbox_env",
        "repair_check",
    ]
    # Sandboxes (ADR-0011)
    sandbox_postgres_url: str | None = None  # process sandbox: admin DSN for ephemeral databases (local/test)
    sandbox_redis_url: str | None = None  # process sandbox: shared Redis (local/test)
    sandbox_inherit_site_packages: bool = False  # process sandbox: reuse the runner's Python packages (offline dev)
    pip_index_url: str | None = None  # package mirror (CodeArtifact/PyPI proxy)
    npm_registry: str | None = None
    mirror_hosts: list[str] = []  # egress allowlist for builds
    build_cache_dir: str = ".local/runner-cache"
    runner_image: str = "ghcr.io/reliability-engineer/runner-agent:latest"
    otel_autoinstrument_image: str = "ghcr.io/reliability-engineer/otel-autoinstrument:latest"
    otel_collector_image: str = "otel/opentelemetry-collector-contrib:0.116.1"
    sandbox_postgres_image: str = "postgres:16.6"
    sandbox_redis_image: str = "redis:7.4.1"
    curl_image: str = "curlimages/curl:8.11.1"
    semgrep_config: str = "p/default"  # security gate; point at a mirrored rules file when offline
    http_timeout_s: float = 30.0
    max_raw_upload_bytes: int = 64 * 1024 * 1024

    @model_validator(mode="after")
    def _no_unisolated_in_production(self) -> Config:
        if self.env in ("staging", "production") and self.driver != "docker":
            raise ValueError(
                "RA_DRIVER must be docker (gVisor) outside local/test — the process driver has no isolation"
            )
        return self


@lru_cache
def get_config() -> Config:
    return Config()
