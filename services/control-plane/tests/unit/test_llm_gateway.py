"""LLM gateway (ADR-0019): routing, redaction, budgets, retries, schema validation, metering,
and the Anthropic adapter's request shape (structured outputs, effort, refusal fallback)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from reliability.llm import gateway as gw
from reliability.llm.gateway import Gateway, NullSink, SchemaInvalid
from reliability.llm.providers import AnthropicClient, FakeClient
from reliability.llm.redact import redact
from reliability.llm.types import Budget, BudgetExhausted, LLMRefusal, LLMRequest, LLMResponse, LLMUnavailable
from reliability.platform.settings import Settings

ORG = uuid.uuid4()
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
}


def _gateway(client: Any) -> Gateway:
    return Gateway(client, Settings(llm_provider="fake"))


async def _call(g: Gateway, req: LLMRequest, sink: Any = None, budget: Budget | None = None, **kw: Any) -> Any:
    return await g.structured(
        req, org_id=ORG, subject_id=None, budget=budget or Budget(100_000), sink=sink or NullSink(), **kw
    )


def test_redaction_patterns() -> None:
    text = (
        "key AKIAABCDEFGHIJKLMNOP token ghp_abcdefghijklmnopqrstuvwxyz0123 "
        "dsn postgres://app:hunter22@db:5432/x password=supersecret sk-ant-api03-abcdefghijklmn "
        "Authorization: Bearer abcdefghijklmnopqrstuv"
    )
    out = redact(text, ["my-own-secret-value"])
    for leaked in (
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghij",
        "hunter22",
        "supersecret",
        "sk-ant-api03",
        "abcdefghijklmnopqrstuv",
    ):
        assert leaked not in out
    assert redact("value my-own-secret-value here", ["my-own-secret-value"]) == "value [REDACTED_SECRET] here"


async def test_secrets_are_redacted_before_the_provider_sees_them() -> None:
    fake = FakeClient()
    fake.register("diagnosis.triage", lambda r: {"answer": 1})
    g = _gateway(fake)
    req = LLMRequest(
        "diagnosis.triage",
        "system with sk-ant-api03-abcdefghijklmnopq",
        [{"role": "user", "content": [{"type": "text", "text": "db pw is tenant-secret-123"}]}],
        SCHEMA,
    )
    assert await _call(g, req, secret_values=["tenant-secret-123"]) == {"answer": 1}
    sent = json.dumps([fake.calls[0].system, fake.calls[0].messages])
    assert "tenant-secret-123" not in sent and "sk-ant-api03" not in sent
    assert "[REDACTED_SECRET]" in sent


async def test_routing_uses_purpose_tier_and_caps_max_tokens() -> None:
    seen: dict[str, Any] = {}

    class Spy(FakeClient):
        async def complete(self, request: LLMRequest, *, model: str, effort: Any, max_tokens: int) -> LLMResponse:
            seen.update(model=model, effort=effort, max_tokens=max_tokens)
            return await super().complete(request, model=model, effort=effort, max_tokens=max_tokens)

    spy = Spy()
    spy.register("diagnosis.hypothesize", lambda r: {"answer": 2})
    g = _gateway(spy)
    await _call(
        g, LLMRequest("diagnosis.hypothesize", "s", [{"role": "user", "content": "x"}], SCHEMA, max_tokens=999_999)
    )
    assert seen == {"model": "claude-opus-5-5", "effort": "high", "max_tokens": 32000}
    with pytest.raises(ValueError, match="unknown LLM purpose"):
        await _call(g, LLMRequest("made.up", "s", [], SCHEMA))


async def test_schema_reask_then_schema_invalid_is_metered() -> None:
    fake = FakeClient()
    answers = iter([{"answer": "not-an-int"}, {"answer": 7}])
    fake.register("diagnosis.triage", lambda r: next(answers))
    sink = NullSink()
    g = _gateway(fake)
    assert await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), sink) == {
        "answer": 7
    }
    assert "does not match the required schema" in fake.calls[1].messages[-1]["content"]

    fake.register("diagnosis.triage", lambda r: {"answer": "never"})
    with pytest.raises(SchemaInvalid):
        await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), sink)
    assert sink.records[-1].outcome == "schema_invalid"
    assert [r.outcome for r in sink.records].count("ok") == 4


async def test_open_schema_goes_into_system_prompt() -> None:
    fake = FakeClient()
    fake.register("discovery.synthesize", lambda r: {"workflows": []})
    g = _gateway(fake)
    schema = {"type": "object", "properties": {"workflows": {"type": "array"}}}
    await _call(
        g, LLMRequest("discovery.synthesize", "base", [{"role": "user", "content": "x"}], schema, constrained=False)
    )
    assert "matches this JSON Schema" in fake.calls[0].system and fake.calls[0].constrained is False


async def test_budget_and_quota_denials() -> None:
    fake = FakeClient()
    fake.register("diagnosis.triage", lambda r: {"answer": 1})
    g = _gateway(fake)
    sink = NullSink()
    with pytest.raises(BudgetExhausted):
        await _call(
            g,
            LLMRequest("diagnosis.triage", "s" * 4000, [{"role": "user", "content": "x"}], SCHEMA),
            sink,
            budget=Budget(100),
        )
    assert sink.records[-1].outcome == "budget_denied" and not fake.calls

    class NoQuota(NullSink):
        async def allow(self, org_id: uuid.UUID, estimated_tokens: int) -> bool:
            return False

    with pytest.raises(BudgetExhausted):
        await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), NoQuota())

    budget = Budget(100_000)
    await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), budget=budget)
    assert budget.spent > 0


async def test_transient_errors_retry_then_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    attempts = {"n": 0}

    def flaky(_: LLMRequest) -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMUnavailable("overloaded")
        return {"answer": 3}

    fake = FakeClient()
    fake.register("diagnosis.triage", flaky)
    g = _gateway(fake)
    assert await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA)) == {
        "answer": 3
    }

    def down(_: LLMRequest) -> Any:
        raise LLMUnavailable("down")

    fake.register("diagnosis.triage", down)
    sink = NullSink()
    with pytest.raises(LLMUnavailable):
        await _call(g, LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), sink)
    assert sink.records[-1].outcome == "error"


async def test_refusal_is_metered_and_raised() -> None:
    def refuse(_: LLMRequest) -> Any:
        raise LLMRefusal("cyber", "declined")

    fake = FakeClient()
    fake.register("diagnosis.triage", refuse)
    sink = NullSink()
    with pytest.raises(LLMRefusal):
        await _call(
            _gateway(fake), LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "x"}], SCHEMA), sink
        )
    assert sink.records[-1].outcome == "refused"


# ------------------------------------------------------------ Anthropic ----
class _FakeMessages:
    def __init__(self, message: Any) -> None:
        self.message, self.calls = message, []

    async def create(self, **params: Any) -> Any:
        self.calls.append(("create", params))
        return self.message

    def stream(self, **params: Any) -> Any:
        self.calls.append(("stream", params))
        outer = self

        class _Stream:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def get_final_message(self) -> Any:
                return outer.message

        return _Stream()


def _message(stop_reason: str = "end_turn", text: str = '{"answer": 5}') -> Any:
    return SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber", explanation="no") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=3, iterations=None),
    )


def _client(message: Any) -> tuple[AnthropicClient, _FakeMessages]:
    client = AnthropicClient("sk-ant-test", 30)
    fake = _FakeMessages(message)
    client._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))  # type: ignore[assignment]
    return client, fake


async def test_anthropic_request_shape() -> None:
    client, fake = _client(_message())
    req = LLMRequest("diagnosis.triage", "sys", [{"role": "user", "content": "q"}], SCHEMA)
    resp = await client.complete(req, model="claude-opus-5-5", effort="high", max_tokens=4000)
    kind, params = fake.calls[0]
    assert kind == "create"
    assert params["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in params["betas"]
    assert "temperature" not in params and "thinking" not in params and "tool_choice" not in params
    assert resp.text == '{"answer": 5}' and resp.cached_tokens == 3 and resp.total_tokens == 15

    # Long outputs stream; models without fallback support get no fallback params; open schemas are not constrained.
    req = LLMRequest("repair.code", "sys", [{"role": "user", "content": "q"}], SCHEMA, constrained=False)
    await client.complete(req, model="claude-haiku-5-5", effort="low", max_tokens=64000)
    kind, params = fake.calls[1]
    assert kind == "stream" and "fallbacks" not in params and params["betas"] == []
    assert "format" not in params["output_config"]


async def test_anthropic_tools_are_strict_with_auto_choice() -> None:
    from reliability.llm.types import ToolSpec

    client, fake = _client(_message())
    tool = ToolSpec("read_file", "Read a file", {"type": "object", "additionalProperties": False, "properties": {}})
    await client.complete(
        LLMRequest("diagnosis.investigate", "s", [{"role": "user", "content": "q"}], tools=[tool]),
        model="claude-opus-5-5",
        effort="high",
        max_tokens=4000,
    )
    params = fake.calls[0][1]
    assert params["tools"][0]["strict"] is True and params["tool_choice"] == {"type": "auto"}


async def test_anthropic_refusal_maps_to_llm_refusal() -> None:
    client, _ = _client(_message("refusal", ""))
    with pytest.raises(LLMRefusal) as exc:
        await client.complete(
            LLMRequest("diagnosis.triage", "s", [{"role": "user", "content": "q"}]),
            model="claude-opus-5-5",
            effort="low",
            max_tokens=100,
        )
    assert exc.value.category == "cyber"


def test_default_gateway_uses_fake_without_credentials() -> None:
    gw.set_gateway(None)
    gw._default_gateway.cache_clear()
    assert isinstance(gw.get_gateway().client, FakeClient)
