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
    job_types: list[str] = ["functional", "load", "probe", "verification", "test_validation"]
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
