"""Workflow IR validation: JSON Schema + semantic reference checks (ADR-0013).

The schema is shared with TypeScript (packages/workflow-ir/schema.json); this package keeps an
identical copy so the control plane ships without the monorepo (a test asserts they match).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from reliability.platform.errors import AppError

SCHEMA_PATH = Path(__file__).with_name("workflow_ir_schema.json")
TEMPLATE_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
BUILTINS = {"base_url", "vu", "iter"}


@lru_cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def _templates(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(TEMPLATE_RE.findall(value))
    if isinstance(value, dict):
        return set().union(*(_templates(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_templates(v) for v in value)) if value else set()
    return set()


def validation_errors(ir: dict[str, Any]) -> list[str]:
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(_validator().iter_errors(ir), key=lambda e: list(e.absolute_path))
    ]
    if errors:
        return errors[:20]
    defined = set(BUILTINS) | set((ir.get("data") or {}).get("vars", {}))
    for pre in ir.get("preconditions", []):
        if pre.get("api"):
            defined |= set(pre["api"].get("extract", {}))
    seen: set[str] = set()
    sequences = [("steps", ir.get("steps", [])), ("api_equivalent", ir.get("api_equivalent", []))]
    step_ids: set[str] = set()
    for name, steps in sequences:
        seen = set()
        scope = set(defined)
        for i, step in enumerate(steps):
            if step["id"] in seen:
                errors.append(f"{name}/{i}: duplicate step id {step['id']}")
            seen.add(step["id"])
            step_ids.add(step["id"])
            missing = _templates(step) - scope
            if missing:
                errors.append(f"{name}/{i}: undefined variable(s) {sorted(missing)}")
            scope |= set(step.get("extract", {}))
            if name == "steps" and "action" in step:
                if step["action"] in ("click", "fill", "select", "check") and "locator" not in step:
                    errors.append(f"{name}/{i}: {step['action']} needs a locator")
                if step["action"] == "navigate" and "url" not in step:
                    errors.append(f"{name}/{i}: navigate needs a url")
                if step["action"] == "expect" and "assertion" not in step:
                    errors.append(f"{name}/{i}: expect needs an assertion")
    if ir["kind"] == "browser" and not ir.get("steps"):
        errors.append("browser workflows need steps")
    if ir["kind"] == "api" and not (ir.get("steps") or ir.get("api_equivalent")):
        errors.append("api workflows need steps")
    if ir["kind"] == "api" and any("action" in s for s in ir.get("steps", [])):
        errors.append("api workflows may only contain request steps")
    for inv in ir.get("invariants", []):
        if inv["step_ref"] not in step_ids:
            errors.append(f"invariant {inv['id']}: step_ref {inv['step_ref']} not found")
    return errors


def validate_ir(ir: dict[str, Any]) -> dict[str, Any]:
    errors = validation_errors(ir)
    if errors:
        raise AppError("VALIDATION_FAILED", "invalid workflow IR", {"errors": errors})
    return ir


def api_steps(ir: dict[str, Any]) -> list[dict[str, Any]]:
    """Request sequence used for load tests: ``api_equivalent`` or the request steps of api workflows."""
    if ir.get("api_equivalent"):
        return list(ir["api_equivalent"])
    return [s for s in ir.get("steps", []) if "request" in s]


def secret_names(ir: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for pre in ir.get("preconditions", []):
        for key in ("credential_ref", "username_ref"):
            if pre.get(key):
                names.add(pre[key].split(":", 1)[1])
    for spec in (ir.get("data") or {}).get("vars", {}).values():
        if "secret" in spec:
            names.add(spec["secret"])
    return names
