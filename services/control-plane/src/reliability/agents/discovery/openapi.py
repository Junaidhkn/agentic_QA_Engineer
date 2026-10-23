"""OpenAPI 3.0/3.1 import (FR-7): operations with request-body examples and security."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from reliability.agents.discovery.routes import METHODS, normalize


class OpenAPIError(ValueError):
    pass


@dataclass
class Operation:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    secured: bool
    body_fields: dict[str, str] = field(default_factory=dict)
    body_example: Any = None
    tags: list[str] = field(default_factory=list)


def load(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise OpenAPIError(f"not valid JSON or YAML: {exc}") from exc
    if not isinstance(doc, dict) or not str(doc.get("openapi", "")).startswith("3."):
        raise OpenAPIError("only OpenAPI 3.0 and 3.1 documents are supported")
    if not isinstance(doc.get("paths"), dict):
        raise OpenAPIError("document has no paths")
    return doc


def _deref(doc: dict[str, Any], node: Any, depth: int = 0) -> Any:
    if isinstance(node, dict) and "$ref" in node and depth < 10:
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            return {}
        cur: Any = doc
        for part in ref[2:].split("/"):
            cur = cur.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(cur, dict) else {}
        return _deref(doc, cur, depth + 1)
    return node


def _fields(doc: dict[str, Any], schema: Any) -> dict[str, str]:
    schema = _deref(doc, schema)
    if not isinstance(schema, dict):
        return {}
    for combinator in ("allOf",):
        if combinator in schema:
            merged: dict[str, str] = {}
            for part in schema[combinator]:
                merged.update(_fields(doc, part))
            return merged
    required = set(schema.get("required") or [])
    out: dict[str, str] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if name not in required:
            continue
        prop = _deref(doc, prop)
        t = prop.get("type") if isinstance(prop, dict) else None
        fmt = prop.get("format") if isinstance(prop, dict) else None
        out[name] = (
            "email" if fmt == "email" else {"integer": "int", "number": "float", "boolean": "bool"}.get(str(t), "str")
        )
    return out


def operations(doc: dict[str, Any]) -> list[Operation]:
    global_security = bool(doc.get("security"))
    ops: list[Operation] = []
    for path, item in doc["paths"].items():
        item = _deref(doc, item)
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.upper() not in METHODS or not isinstance(op, dict):
                continue
            body = _deref(doc, op.get("requestBody") or {})
            media = (body.get("content") or {}).get("application/json") or {}
            example = media.get("example")
            if example is None and isinstance(media.get("examples"), dict) and media["examples"]:
                example = _deref(doc, next(iter(media["examples"].values()))).get("value")
            secured = bool(op["security"]) if "security" in op else global_security
            ops.append(
                Operation(
                    method=method.upper(),
                    path=normalize(path),
                    operation_id=op.get("operationId"),
                    summary=op.get("summary"),
                    secured=secured,
                    body_fields=_fields(doc, media.get("schema")),
                    body_example=example,
                    tags=list(op.get("tags") or []),
                )
            )
    return ops
