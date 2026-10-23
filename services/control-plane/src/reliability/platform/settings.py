"""Typed configuration loaded from environment variables (prefix ``RE_``).

Every variable is documented in the repository root ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RE_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "staging", "production"] = "local"
    service_name: str = "reliability-control-plane"
    log_level: str = "INFO"

    # Public URLs
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Postgres: app role (RLS enforced), system role (BYPASSRLS, cross-tenant jobs only),
    # migration role (schema owner).
    database_url: str = "postgresql+psycopg://re_app:re_app@localhost:5432/reliability"
    database_system_url: str = "postgresql+psycopg://re_system:re_system@localhost:5432/reliability"
    database_migration_url: str = "postgresql+psycopg://re_owner:re_owner@localhost:5432/reliability"
    database_pool_size: int = 10

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None

    # Object storage (ADR-0005)
    object_store: Literal["s3", "local"] = "local"
    s3_bucket: str = "re-artifacts-local"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    local_store_path: str = ".local/artifacts"
    presign_ttl_seconds: int = 900

    # Encryption of project secrets (KMS envelope; local master key for dev/test)
    kms: Literal["aws", "local"] = "local"
    kms_key_id: str | None = None
    local_master_key: SecretStr = SecretStr("ZGV2LW9ubHktbWFzdGVyLWtleS0zMi1ieXRlcyEhISE=")

    # Tokens and hashing
    api_key_pepper: SecretStr = SecretStr("dev-pepper-change-me")
    token_signing_key: SecretStr = SecretStr("dev-token-signing-key-change-me-0123456789")
    runner_signing_key: SecretStr = SecretStr("dev-runner-signing-key-change-me-0123456789")

    # Identity provider (ADR-0018) — generic OIDC JWT verification
    idp_issuer: str | None = None
    idp_audience: str | None = None
    idp_jwks_url: str | None = None
    dev_auth_enabled: bool = True  # accepts "Bearer dev:<email>" only when env is local/test

    # GitHub App (ADR-0017)
    github_app_id: str | None = None
    github_app_slug: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None
    github_api_url: str = "https://api.github.com"

    # Targets: allow private/loopback hosts (local development only; rejected in staging/production)
    allow_private_targets: bool = False

    # LLM gateway (ADR-0019)
    llm_provider: Literal["anthropic", "fake"] = "fake"
    anthropic_api_key: SecretStr | None = None
    llm_model_tier_s: str = "claude-haiku-5-5"
    llm_model_tier_m: str = "claude-sonnet-5-5"
    llm_model_tier_l: str = "claude-opus-5-5"
    llm_timeout_seconds: float = 180.0

    # Telemetry backends (ADR-0014)
    mimir_url: str = "http://localhost:9009/prometheus"
    tempo_url: str = "http://localhost:3200"
    loki_url: str = "http://localhost:3100"
    otlp_public_url: str = "http://localhost:4319"
    otel_collector_url: str = "http://localhost:4318"  # internal collector behind the OTLP gateway
    telemetry_query_timeout_s: float = 15.0

    # Execution plane
    runner_lease_ttl_seconds: int = 15
    runner_long_poll_seconds: int = 20
    egress_ips: list[str] = Field(default_factory=list)

    # Email
    email_provider: Literal["log", "ses"] = "log"
    email_from: str = "Reliability Engineer <no-reply@localhost>"

    # Error tracking
    sentry_dsn: str | None = None

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.env in ("staging", "production"):
            insecure = []
            if self.dev_auth_enabled:
                insecure.append("RE_DEV_AUTH_ENABLED must be false")
            if self.api_key_pepper.get_secret_value().startswith("dev-"):
                insecure.append("RE_API_KEY_PEPPER must be set")
            if self.token_signing_key.get_secret_value().startswith("dev-"):
                insecure.append("RE_TOKEN_SIGNING_KEY must be set")
            if self.runner_signing_key.get_secret_value().startswith("dev-"):
                insecure.append("RE_RUNNER_SIGNING_KEY must be set")
            if self.allow_private_targets:
                insecure.append("RE_ALLOW_PRIVATE_TARGETS must be false")
            if self.kms == "local":
                insecure.append("RE_KMS must be aws")
            if self.llm_provider == "fake":
                insecure.append("RE_LLM_PROVIDER must not be fake")
            if insecure:
                raise ValueError("insecure configuration for " + self.env + ": " + "; ".join(insecure))
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def dev_auth_active(self) -> bool:
        return self.dev_auth_enabled and self.env in ("local", "test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
