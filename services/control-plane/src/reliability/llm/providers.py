"""LLM providers behind the ``LLMClient`` port.

* ``AnthropicClient`` — Claude via the official SDK (beta Messages endpoint for server-side
  refusal fallbacks), structured outputs via ``output_config.format``, adaptive thinking with
  explicit effort, prompt caching, streaming for long outputs.
* ``FakeClient`` — deterministic responses from registered handlers; used in tests and in
  local development without an API key (RE_LLM_PROVIDER=fake).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from reliability.llm.types import Effort, LLMRefusal, LLMRequest, LLMResponse, LLMUnavailable


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest, *, model: str, effort: Effort, max_tokens: int) -> LLMResponse: ...


FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5")
STREAM_THRESHOLD_TOKENS = 16000


class AnthropicClient:
    def __init__(self, api_key: str, timeout_s: float, enable_fallbacks: bool = True) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)
        self._fallbacks = enable_fallbacks

    async def complete(self, request: LLMRequest, *, model: str, effort: Effort, max_tokens: int) -> LLMResponse:
        anthropic = self._anthropic
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
            "messages": request.messages,
            "output_config": {"effort": effort},
        }
        if request.schema is not None and request.constrained:
            params["output_config"]["format"] = {"type": "json_schema", "schema": request.schema}
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema, "strict": True}
                for t in request.tools
            ]
            params["tool_choice"] = {"type": "auto"}
        betas: list[str] = []
        if self._fallbacks and model in FALLBACK_MODELS:
            # Server-side refusal fallback routed by refusal category (no model list to maintain).
            betas.append("server-side-fallback-2026-07-01")
            params["fallbacks"] = "default"
        started = time.monotonic()
        try:
            if max_tokens > STREAM_THRESHOLD_TOKENS:
                async with self._client.beta.messages.stream(betas=betas, **params) as stream:
                    message = await stream.get_final_message()
            else:
                message = await self._client.beta.messages.create(betas=betas, **params)
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("rate limited") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 529:
                raise LLMUnavailable(f"provider error {exc.status_code}") from exc
            raise
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable("connection error") from exc
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusal(getattr(details, "category", None), getattr(details, "explanation", None))
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if block.type == "text":
                blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)})
            elif block.type in ("thinking", "redacted_thinking"):
                # Kept verbatim: tool-use continuations must send thinking blocks back unchanged.
                blocks.append(block.model_dump(exclude_none=True))
        usage = message.usage
        iterations = getattr(usage, "iterations", None) or []
        return LLMResponse(
            model=message.model,
            content=blocks,
            stop_reason=str(message.stop_reason),
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            served_by_fallback=any(getattr(i, "type", "") == "fallback_message" for i in iterations),
        )


FakeHandler = Callable[[LLMRequest], Any | Awaitable[Any]]


class FakeClient:
    """Deterministic stand-in. Handlers return either a dict/list (JSON output) or a full
    ``LLMResponse``. Unregistered purposes return an empty JSON object."""

    def __init__(self) -> None:
        self.handlers: dict[str, FakeHandler] = {}
        self.calls: list[LLMRequest] = []

    def register(self, purpose: str, handler: FakeHandler) -> None:
        self.handlers[purpose] = handler

    async def complete(self, request: LLMRequest, *, model: str, effort: Effort, max_tokens: int) -> LLMResponse:
        self.calls.append(request)
        handler = self.handlers.get(request.purpose)
        result: Any = {} if handler is None else handler(request)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, LLMResponse):
            return result
        text = json.dumps(result)
        prompt_size = sum(len(json.dumps(m)) for m in request.messages) + len(request.system)
        return LLMResponse(
            model=f"fake/{model}",
            content=[{"type": "text", "text": text}],
            stop_reason="end_turn",
            input_tokens=prompt_size // 4,
            output_tokens=len(text) // 4,
        )
