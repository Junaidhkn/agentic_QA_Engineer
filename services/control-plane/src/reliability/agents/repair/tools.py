"""Workspace tools for the repair agent (agent-workflows §6).

Reads are limited to the snapshot; writes go through the same path policy the verification
``diff_policy`` gate applies, plus a phase rule: while writing the regression test only test
files may change, and once the test is confirmed failing on base it is frozen. There is no tool
that runs arbitrary commands, reaches the network, touches Git or calls the API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.tools import Tool, ToolError, ToolRegistry
from reliability.agents.common.untrusted import wrap
from reliability.agents.repair.commands import Commands
from reliability.agents.repair.workspace import PatchError, Workspace

Phase = Literal["test", "code"]
PathPolicy = Callable[[str], str | None]
EvidenceReader = Callable[[str], Awaitable[str | None]]
MAX_READ = 60_000


class ListArgs(BaseModel):
    prefix: str = Field(default="", max_length=300, description="directory prefix, e.g. 'app/'")


class ReadArgs(BaseModel):
    path: str = Field(max_length=300)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchArgs(BaseModel):
    pattern: str = Field(max_length=200, description="Python regular expression")
    path_prefix: str = Field(default="", max_length=300)


class WriteArgs(BaseModel):
    path: str = Field(max_length=300)
    content: str = Field(max_length=200_000, description="the complete new file content")


class PatchArgs(BaseModel):
    diff: str = Field(max_length=200_000, description="unified diff with a/ and b/ prefixes")


class EvidenceArgs(BaseModel):
    evidence_id: str = Field(max_length=64)


def build_tools(
    budget: GraphBudget,
    ws: Workspace,
    phase: Phase,
    commands: Commands,
    path_policy: PathPolicy,
    frozen: set[str],
    evidence: EvidenceReader,
) -> ToolRegistry:
    reg = ToolRegistry("repair", {"read_code", "read_tenant_data", "sandbox_write"}, budget)

    def check_write(path: str) -> None:
        if (problem := path_policy(path)) is not None:
            raise ToolError(problem)
        if phase == "test" and not commands.is_test_path(path):
            raise ToolError(f"{path}: only test files may be written while writing the regression test")
        if phase == "code" and path in frozen:
            raise ToolError(f"{path}: the regression test is frozen once it fails on the base commit")

    async def list_files(a: ListArgs) -> str:
        names = ws.list_paths(a.prefix)
        return "\n".join(names[:400]) + (f"\n… {len(names) - 400} more" if len(names) > 400 else "")

    async def read_file(a: ReadArgs) -> str:
        text = ws.read(a.path)
        if text is None:
            raise ToolError(f"{a.path}: not found")
        lines = text.splitlines()
        end = min(a.end_line or len(lines), len(lines))
        body = "\n".join(f"{n:>5}  {line}" for n, line in enumerate(lines[a.start_line - 1 : end], a.start_line))
        return wrap("file", a.path, body, MAX_READ)

    async def search_code(a: SearchArgs) -> str:
        try:
            hits = ws.search(a.pattern, a.path_prefix)
        except PatchError as exc:
            raise ToolError(str(exc)) from exc
        return wrap("search", a.pattern, "\n".join(f"{p}:{n}: {t}" for p, n, t in hits) or "no matches")

    async def write_file(a: WriteArgs) -> str:
        check_write(a.path)
        ws.write(a.path, a.content)
        return f"wrote {a.path} ({len(a.content.splitlines())} lines)"

    async def apply_patch(a: PatchArgs) -> str:
        trial = Workspace(ws.snapshot, dict(ws.overlay))
        try:
            touched = trial.apply_patch(a.diff)
        except PatchError as exc:
            raise ToolError(str(exc)) from exc
        for path in touched:
            check_write(path)
        ws.overlay = trial.overlay
        return f"patched {', '.join(touched)}"

    async def get_evidence(a: EvidenceArgs) -> str:
        text = await evidence(a.evidence_id)
        if text is None:
            raise ToolError("unknown evidence id")
        return wrap("evidence", a.evidence_id, text)

    for name, desc, args, perm, fn in (
        ("list_files", "List files in the repository snapshot.", ListArgs, "read_code", list_files),
        ("read_file", "Read a file (with line numbers).", ReadArgs, "read_code", read_file),
        ("search_code", "Search the repository with a regular expression.", SearchArgs, "read_code", search_code),
        ("write_file", "Create or replace a whole file.", WriteArgs, "sandbox_write", write_file),
        ("apply_patch", "Apply a unified diff to the workspace.", PatchArgs, "sandbox_write", apply_patch),
        ("get_evidence", "Read one evidence item from the diagnosis.", EvidenceArgs, "read_tenant_data", get_evidence),
    ):
        reg.register(Tool(name, desc, args, perm, fn))  # type: ignore[arg-type]
    return reg
