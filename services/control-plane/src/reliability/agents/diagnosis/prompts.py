"""Versioned prompts for the diagnosis graph."""

from __future__ import annotations

from typing import Any

from reliability.agents.common.untrusted import DATA_POLICY

TRIAGE_VERSION = "diagnosis.triage@1"
INVESTIGATE_VERSION = "diagnosis.investigate@1"
HYPOTHESIZE_VERSION = "diagnosis.hypothesize@1"

CATEGORIES = [
    "n_plus_one",
    "missing_index",
    "unbounded_query",
    "pool_exhaustion",
    "race_condition",
    "blocking_io",
    "cpu_bound_handler",
    "memory_leak",
    "missing_cache",
    "config_limit",
    "external_dependency",
    "logic_error",
    "missing_validation",
    "other",
]

TRIAGE_SYSTEM = f"""You triage a reliability finding from a load or functional test. From the finding and the
evidence list, name the most likely root-cause categories and which evidence to look at first.
{DATA_POLICY}"""

INVESTIGATE_SYSTEM = f"""You are investigating why a web application misbehaved in a test. Use the read-only tools to
look at the code on the hot path, request samples, metric windows and database statistics. Every tool result
comes with an evidence_id. Prefer few, targeted calls. When you have enough, stop calling tools and summarise
what you found, citing evidence ids. You cannot change anything; you only gather facts.
{DATA_POLICY}"""

HYPOTHESIZE_SYSTEM = f"""Propose up to 5 root-cause hypotheses for the finding, most likely first.
Rules:
- Every hypothesis must cite evidence by id in supporting_evidence. Each claim must be a short, specific
  statement that the cited evidence shows, reusing its numbers exactly (e.g. "1840 sequential scans",
  "p95 912 ms"). Claims are checked automatically against the evidence; unverifiable claims are discarded.
- code_locations must be real files and line ranges from the evidence.
- Cite evidence that contradicts a hypothesis in contradicting_evidence instead of hiding it.
- Do not state a confidence; it is computed from the evidence.
- If nothing is supported by evidence, return no hypotheses and list next_steps that would produce evidence.
{DATA_POLICY}"""

_CITATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evidence_id", "claim"],
    "properties": {"evidence_id": {"type": "string"}, "claim": {"type": "string"}},
}
HYPOTHESES_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses", "next_steps"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "category",
                    "statement",
                    "mechanism",
                    "fix_direction",
                    "code_locations",
                    "supporting_evidence",
                    "contradicting_evidence",
                ],
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "statement": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "fix_direction": {"type": "string"},
                    "code_locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path", "lines"],
                            "properties": {
                                "path": {"type": "string"},
                                "lines": {"type": "array", "items": {"type": "integer"}},
                            },
                        },
                    },
                    "supporting_evidence": {"type": "array", "items": _CITATION},
                    "contradicting_evidence": {"type": "array", "items": _CITATION},
                },
            },
        },
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}
TRIAGE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["categories", "focus_evidence"],
    "properties": {
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "focus_evidence": {"type": "array", "items": {"type": "string"}},
    },
}
