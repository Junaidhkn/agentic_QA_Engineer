"""Agent tool registry (agent-workflows §2, ADR-0007).

Tools are registered per graph with typed (Pydantic) arguments and a permission class. The
registry refuses classes a graph was not granted, validates arguments before execution,
charges the budget, and reports every call (arguments hashed) to an audit callback. There is
deliberately no permission class that approves, merges, deploys or changes targets/quotas.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from reliability.agents.common.budget import GraphBudget
from reliability.llm.types import ToolSpec

Permission = Literal["read_tenant_data", "read_telemetry", "read_code", "sandbox_exec", "sandbox_write"]
AuditHook = Callable[[str, str, str], Awaitable[None]]  # (graph, tool, args_sha256)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    permission: Permission
    run: Callable[[Any], Awaitable[str]]

    def spec(self) -> ToolSpec:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        return ToolSpec(self.name, self.description, schema)


class ToolError(Exception):
    pass


class ToolRegistry:
    def __init__(self, graph: str, allowed: set[Permission], budget: GraphBudget, audit: AuditHook | None = None):
        self.graph, self.allowed, self.budget, self.audit = graph, allowed, budget, audit
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.permission not in self.allowed:
            raise PermissionError(f"{self.graph} may not register {tool.permission} tool {tool.name}")
        self._tools[tool.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    async def call(self, name: str, raw_args: dict[str, Any]) -> str:
        """Returns the tool output (to be wrapped as untrusted data by the caller), or an error
        string the model can react to. Unknown tools and invalid arguments never execute."""
        tool = self._tools.get(name)
        if tool is None:
            return f"error: unknown tool {name}"
        self.budget.tool_call()
        try:
            args = tool.args.model_validate(raw_args)
        except ValidationError as exc:
            return f"error: invalid arguments: {exc.errors()[:3]}"
        if self.audit:
            digest = hashlib.sha256(json.dumps(raw_args, sort_keys=True, default=str).encode()).hexdigest()
            await self.audit(self.graph, name, digest)
        try:
            return await tool.run(args)
        except ToolError as exc:
            return f"error: {exc}"
