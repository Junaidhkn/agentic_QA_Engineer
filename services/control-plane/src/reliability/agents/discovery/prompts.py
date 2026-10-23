"""Versioned prompts for the discovery graph (agent-workflows §7.5). Bump the version when the
text changes; it is recorded in the discovery run stats."""

from __future__ import annotations

from typing import Any

from reliability.agents.common.untrusted import DATA_POLICY

SYNTHESIZE_VERSION = "discovery.synthesize@1"
CRITICALITY_VERSION = "discovery.criticality@1"

SYNTHESIZE_SYSTEM = f"""You design end-to-end user workflows for automated functional and load testing of a web
application. You receive the application's route table (extracted deterministically), the
workflows already derived from it, and excerpts of handler source code.

Propose ADDITIONAL workflows that a real user performs across several endpoints (for example
sign up → add to cart → check out), and IMPROVED versions of existing workflows whose request
bodies or status expectations are wrong according to the code. Rules:
- Use only methods and paths present in the route table; path parameters are filled with
  variables extracted from earlier steps ("{{{{ order_id }}}}"), never invented literals.
- Every request body must match the fields the handler reads. Generated values use data.vars
  (faker/range/value). Never put real credentials in a workflow; secrets are referenced as
  {{"secret": "NAME"}} variables.
- Set source.type to "static" and cite evidence for every step: "route:METHOD /path" and
  "file:<path>:<line>".
- Mark workflows that delete or irreversibly change data with "destructive": true.
- Do not repeat an existing workflow unchanged. Return at most 12 workflows.
- Add an invariant (type "concurrency_probe") only where the code shows a limited resource
  that concurrent requests could oversell (stock, coupon redemptions, seats).

{DATA_POLICY}"""

CRITICALITY_SYSTEM = f"""You rank how business-critical each workflow is for the application's users:
"critical" (revenue, sign-in, data loss), "high" (core feature) or "normal". Base the ranking
on what the workflow does; give a one-sentence reason. {DATA_POLICY}"""

SYNTHESIS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["workflows"],
    "properties": {
        "workflows": {"type": "array", "maxItems": 12, "items": {"type": "object"}},
        "notes": {"type": "string"},
    },
}

CRITICALITY_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assessments"],
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "criticality", "reason"],
                "properties": {
                    "key": {"type": "string"},
                    "criticality": {"type": "string", "enum": ["critical", "high", "normal"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}
