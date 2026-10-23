# Vendor choices (Phase 0)

> Concrete vendors behind the adapters named in the ADRs. Each sits behind an internal
> interface so it can be swapped. Credentials are supplied later via `.env` (see `.env.example`).

| Need | Choice | Adapter | ADR | Notes |
|---|---|---|---|---|
| Identity provider | **Clerk** (GitHub social login, email magic link, MFA) | `identity.verifier.OidcVerifier` (generic OIDC JWKS) | 0018 | Any OIDC provider issuing RS256 JWTs works (Auth0, WorkOS, Cognito). Configure a JWT template with `aud=reliability-api` and `email` claim. |
| LLM provider | **Anthropic Claude** — tiers S/M/L = Haiku / Sonnet / Opus (5.5 generation) | `llm.providers.AnthropicClient` | 0019 | Requires zero/limited-retention data terms before production use. `RE_LLM_PROVIDER=fake` for local development without a key. |
| Cloud | **AWS** (ECS Fargate, EC2, RDS, ElastiCache, S3, KMS, Secrets Manager, PrivateLink) | `platform.storage`, `platform.crypto` | 0020 | Terraform in `infra/terraform`. |
| Object storage | **S3** (MinIO locally, filesystem in tests) | `platform.storage.ObjectStore` | 0005 | |
| Secret encryption | **AWS KMS** envelope encryption (local AES-GCM master key in dev) | `platform.crypto.Kms` | — | |
| Email | **Amazon SES** (log driver locally) | `modules.notifications.EmailSender` | — | |
| Error tracking | **Sentry** | `RE_SENTRY_DSN` | — | Optional. |
| Telemetry backends | **Grafana Mimir / Tempo / Loki** (single-binary), `grafana/otel-lgtm` locally | `modules.telemetry.TelemetryQuery` | 0014 | |
| Code hosting | **GitHub App** | `integrations.github.GitHubClient` | 0017 | |

## Cost model inputs

Unit prices change; compute them from the current AWS and Anthropic price sheets when
plans are priced (Phase 11) using the units in operations.md §8:

- execution compute: vCPU-hours per run type (≈1.6 generator-only, ≈4 with sandbox env);
- LLM: tokens per discovery / diagnosis / repair attempt, recorded in `llm_usage` by purpose and model;
- telemetry: active series and spans per run;
- fixed: RDS Multi-AZ, ElastiCache, NAT, LGTM hosts.
