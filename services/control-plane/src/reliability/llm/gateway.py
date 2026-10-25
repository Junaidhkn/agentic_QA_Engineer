"""The LLM gateway (ADR-0019): the only way graph code reaches a model.

Every call goes through ``Gateway.complete``/``Gateway.structured``, which

1. resolves the purpose to a tier, model, effort and output budget (``PURPOSES``),
2. redacts secrets from the prompt (known secret values plus token patterns),
3. checks the graph budget and the org's monthly token quota (``UsageSink.allow``),
4. calls the provider, retrying transient failures with backoff,
5. validates structured output against the request schema and re-asks once on failure,
6. meters the call (``UsageSink.record``) whatever the outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from reliability.llm.providers import AnthropicClient, FakeClient, LLMClient
from reliability.llm.redact import redact
from reliability.llm.types import (
    PURPOSES,
    Budget,
    BudgetExhausted,
    LLMRefusal,
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    PurposeConfig,
    UsageRecord,
    UsageSink,
)
from reliability.platform import metrics
from reliability.platform.settings import Settings, get_settings

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (1.0, 4.0, 10.0)


class SchemaInvalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("model output did not match the schema: " + "; ".join(errors[:3]))
        self.errors = errors


class NullSink:
    """Meters nothing; used by evaluation harnesses and unit tests."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, record: UsageRecord) -> None:
        self.records.append(record)

    async def allow(self, org_id: uuid.UUID, estimated_tokens: int) -> bool:
        return True


def _redact_messages(messages: list[dict[str, Any]], values: Iterable[str]) -> list[dict[str, Any]]:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict) and value.get("type") in ("thinking", "redacted_thinking"):
            return value  # model output replayed verbatim (signed); never contains customer input
        if isinstance(value, str):
            return redact(value, values)
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value

    return [scrub(m) for m in messages]


def _with_schema_instruction(request: LLMRequest) -> str:
    if request.schema is None or request.constrained:
        return request.system
    return (
        request.system
        + "\n\nRespond with one JSON value that matches this JSON Schema, with no prose or code fences:\n"
        + json.dumps(request.schema, separators=(",", ":"))
    )


def _estimate_tokens(request: LLMRequest) -> int:
    return (len(request.system) + sum(len(json.dumps(m)) for m in request.messages)) // 4


def parse_json(text: str) -> Any:
    """Parse model JSON; tolerates a fenced block when a provider ignores the output format."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return json.loads(stripped)


class Gateway:
    def __init__(self, client: LLMClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()

    def model_for(self, config: PurposeConfig) -> str:
        return {
            "S": self.settings.llm_model_tier_s,
            "M": self.settings.llm_model_tier_m,
            "L": self.settings.llm_model_tier_l,
        }[config.tier]

    async def complete(
        self,
        request: LLMRequest,
        *,
        org_id: uuid.UUID,
        subject_id: uuid.UUID | None,
        budget: Budget,
        sink: UsageSink,
        secret_values: Iterable[str] = (),
    ) -> LLMResponse:
        config = PURPOSES.get(request.purpose)
        if config is None:
            raise ValueError(f"unknown LLM purpose {request.purpose!r}")
        model = self.model_for(config)
        max_tokens = min(request.max_tokens or config.max_tokens, config.max_tokens)
        values = [v for v in secret_values if v]
        safe = LLMRequest(
            purpose=request.purpose,
            system=redact(_with_schema_instruction(request), values),
            messages=_redact_messages(request.messages, values),
            schema=request.schema,
            tools=request.tools,
            max_tokens=max_tokens,
            constrained=request.constrained,
        )
        estimate = _estimate_tokens(safe) + max_tokens // 4
        if budget.remaining < estimate // 2 or not await sink.allow(org_id, estimate):
            await sink.record(UsageRecord(org_id, request.purpose, subject_id, model, 0, 0, 0, 0, "budget_denied"))
            metrics.LLM_REQUESTS.labels(request.purpose, "budget_denied").inc()
            raise BudgetExhausted(f"token budget exhausted for {request.purpose}")

        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS_S)):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.client.complete(safe, model=model, effort=config.effort, max_tokens=max_tokens)
            except LLMUnavailable as exc:
                last_error = exc
                metrics.LLM_REQUESTS.labels(request.purpose, "unavailable").inc()
                log.warning("llm unavailable", extra={"purpose": request.purpose, "attempt": attempt})
                continue
            except LLMRefusal:
                await sink.record(UsageRecord(org_id, request.purpose, subject_id, model, 0, 0, 0, 0, "refused"))
                metrics.LLM_REQUESTS.labels(request.purpose, "refused").inc()
                raise
            budget.charge(response.total_tokens)
            metrics.LLM_REQUESTS.labels(request.purpose, "ok").inc()
            metrics.LLM_TOKENS.labels(request.purpose, "input").inc(response.input_tokens)
            metrics.LLM_TOKENS.labels(request.purpose, "output").inc(response.output_tokens)
            metrics.LLM_TOKENS.labels(request.purpose, "cached").inc(response.cached_tokens)
            await sink.record(
                UsageRecord(
                    org_id,
                    request.purpose,
                    subject_id,
                    response.model,
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_tokens,
                    response.latency_ms,
                    "ok",
                )
            )
            return response
        await sink.record(UsageRecord(org_id, request.purpose, subject_id, model, 0, 0, 0, 0, "error"))
        raise LLMUnavailable(str(last_error or "provider unavailable"))

    async def structured(
        self,
        request: LLMRequest,
        *,
        org_id: uuid.UUID,
        subject_id: uuid.UUID | None,
        budget: Budget,
        sink: UsageSink,
        secret_values: Iterable[str] = (),
    ) -> Any:
        """Returns the parsed JSON object, validated against ``request.schema``. Re-asks once
        with the validation errors before raising ``SchemaInvalid``."""
        if request.schema is None:
            raise ValueError("structured() needs a schema")
        validator = Draft202012Validator(request.schema)
        messages = list(request.messages)
        errors: list[str] = []
        for _ in range(2):
            response = await self.complete(
                LLMRequest(
                    request.purpose,
                    request.system,
                    messages,
                    request.schema,
                    request.tools,
                    request.max_tokens,
                    request.constrained,
                ),
                org_id=org_id,
                subject_id=subject_id,
                budget=budget,
                sink=sink,
                secret_values=secret_values,
            )
            try:
                value = parse_json(response.text)
                errors = [
                    f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
                    for e in validator.iter_errors(value)
                ]
            except json.JSONDecodeError as exc:
                errors = [f"invalid JSON: {exc}"]
            if not errors:
                return value
            messages = [
                *messages,
                {"role": "assistant", "content": response.text[:20000] or "(empty)"},
                {
                    "role": "user",
                    "content": "That output does not match the required schema:\n- "
                    + "\n- ".join(errors[:10])
                    + "\nReturn the corrected JSON only.",
                },
            ]
        await sink.record(UsageRecord(org_id, request.purpose, subject_id, "-", 0, 0, 0, 0, "schema_invalid"))
        metrics.LLM_REQUESTS.labels(request.purpose, "schema_invalid").inc()
        raise SchemaInvalid(errors)


_override: Gateway | None = None


def set_gateway(gateway: Gateway | None) -> None:
    """Tests and evaluation harnesses install a gateway backed by a ``FakeClient``."""
    global _override
    _override = gateway


@lru_cache
def _default_gateway() -> Gateway:
    settings = get_settings()
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise RuntimeError("RE_ANTHROPIC_API_KEY is required when RE_LLM_PROVIDER=anthropic")
        client: LLMClient = AnthropicClient(settings.anthropic_api_key.get_secret_value(), settings.llm_timeout_seconds)
    else:
        from reliability.llm.fakes import default_fake_client

        client = default_fake_client()
    return Gateway(client, settings)


def get_gateway() -> Gateway:
    return _override or _default_gateway()


__all__ = ["FakeClient", "Gateway", "NullSink", "SchemaInvalid", "get_gateway", "parse_json", "set_gateway"]
