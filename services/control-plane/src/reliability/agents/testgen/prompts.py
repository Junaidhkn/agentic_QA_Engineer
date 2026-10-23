"""Versioned prompts for the test-generation graph."""

from __future__ import annotations

from typing import Any

from reliability.agents.common.untrusted import DATA_POLICY

REPAIR_VERSION = "testgen.repair@1"

REPAIR_SYSTEM = f"""A generated test for one user workflow failed its dry run against the application. Tests are
compiled deterministically from the workflow's Workflow IR, so you fix the IR, not code.

Return the corrected IR and a one-paragraph rationale. You may change request bodies, query
parameters, headers, extraction JSONPaths, expected status codes, locators, assertion values,
step order and data.vars, and you may add steps that call endpoints listed under "known
endpoints" (for example to obtain an id). Keep the same key, kind and intent; never mark a
workflow non-destructive that deletes data, never add credentials (reference secrets as
{{"secret": "NAME"}}), and never weaken an assertion just to make the test pass when the
failure shows a real application error (5xx): in that case return the IR unchanged and say so.

{DATA_POLICY}"""

REPAIR_OUTPUT: dict[str, Any] = {
    "type": "object",
    "required": ["ir", "rationale"],
    "properties": {"ir": {"type": "object"}, "rationale": {"type": "string"}},
}
