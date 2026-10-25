"""Deterministic fake model used when no provider is configured (RE_LLM_PROVIDER=fake).

Graphs are written so that every LLM step has a deterministic baseline: the fake returns
"no additions" answers, which keeps local development and CI fully functional without an
API key while exercising the same gateway, budget and metering code paths.
"""

from __future__ import annotations

import json
from typing import Any

from reliability.llm.providers import FakeClient
from reliability.llm.types import LLMRequest

PAYLOAD_OPEN, PAYLOAD_CLOSE = "<payload>", "</payload>"


def request_payload(request: LLMRequest) -> Any:
    """The JSON payload an agent embedded in its last user message (``<payload>…</payload>``)."""
    for message in reversed(request.messages):
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content or []]
        for text in texts:
            if PAYLOAD_OPEN in text and PAYLOAD_CLOSE in text:
                return json.loads(text.split(PAYLOAD_OPEN, 1)[1].split(PAYLOAD_CLOSE, 1)[0])
    return None


def _no_workflows(_: LLMRequest) -> dict[str, Any]:
    return {"workflows": [], "notes": "fake model: deterministic baseline only"}


def _no_changes(request: LLMRequest) -> dict[str, Any]:
    payload = request_payload(request) or {}
    return {"ir": payload.get("ir", {}), "rationale": "fake model: no repair proposed"}


def _criticality(request: LLMRequest) -> dict[str, Any]:
    payload = request_payload(request) or {}
    return {
        "assessments": [
            {"key": w["key"], "criticality": "normal", "reason": "fake model: rules decide"}
            for w in payload.get("workflows", [])
        ]
    }


def default_fake_client() -> FakeClient:
    client = FakeClient()
    client.register("discovery.synthesize", _no_workflows)
    client.register("discovery.criticality", _criticality)
    client.register("testgen.repair", _no_changes)
    client.register("diagnosis.triage", lambda r: {"categories": [], "focus_evidence": []})
    client.register("diagnosis.investigate", lambda r: "fake model: no further investigation")
    client.register("diagnosis.hypothesize", lambda r: {"hypotheses": [], "next_steps": []})
    client.register(
        "repair.plan",
        lambda r: {"files": [], "approach": "fake model: no repair proposed", "test_idea": "", "explanation": ""},
    )
    client.register("repair.code", lambda r: "fake model: no changes")
    return client
