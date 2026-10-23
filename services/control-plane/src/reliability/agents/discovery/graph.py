"""Discovery graph (agent-workflows §3).

snapshot → detect_stack → extract_routes → import_openapi → synthesize_baseline →
synthesize_llm → rank_criticality → persist

Every step except ``synthesize_llm`` and the model half of ``rank_criticality`` is
deterministic. Model output is validated (Workflow IR schema, references, and every request
path must exist in the route table); anything invalid is dropped, never repaired by hand. When
the model is unavailable, refuses, or the budget runs out, discovery still completes with the
deterministic baseline (status ``budget_exhausted`` when the budget was the cause).

Human review happens through the workflows API (candidates are never tested until approved),
so this graph has no interrupt.
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.checkpoint import checkpointer
from reliability.agents.common.untrusted import payload, wrap
from reliability.agents.discovery import criticality, prompts, stack, synthesis
from reliability.agents.discovery import openapi as openapi_mod
from reliability.agents.discovery import routes as routes_mod
from reliability.agents.discovery.snapshot import CodeSnapshot
from reliability.llm import BudgetExhausted, Gateway, LLMRefusal, LLMRequest, LLMUnavailable, SchemaInvalid
from reliability.llm.types import UsageSink
from reliability.modules.workflows.ir import validation_errors

MAX_EXCERPT_CHARS = 60_000
MAX_FILE_EXCERPT = 6_000


class DiscoveryPorts(Protocol):
    """Implemented by ``modules/discovery``: data access and persistence for one run."""

    async def snapshot(self) -> tuple[str, CodeSnapshot]: ...
    async def openapi_documents(self) -> list[str]: ...
    async def secret_names(self) -> set[str]: ...
    async def progress(self, step: str, detail: dict[str, Any]) -> None: ...
    async def persist(self, candidates: list[dict[str, Any]], stack: dict[str, Any]) -> dict[str, int]: ...


@dataclass
class DiscoveryDeps:
    ports: DiscoveryPorts
    gateway: Gateway
    sink: UsageSink
    budget: GraphBudget
    org_id: uuid.UUID
    run_id: uuid.UUID
    llm_enabled: bool = True


class DiscoveryState(TypedDict, total=False):
    commit_sha: str
    excluded_files: int
    stack: dict[str, Any]
    routes: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    openapi_errors: list[str]
    candidates: list[dict[str, Any]]
    llm: dict[str, Any]
    budget_exhausted: bool
    persisted: dict[str, int]


_TEMPLATE = re.compile(r"\{\{\s*[a-z_][a-z0-9_]*\s*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}")


def path_shape(path: str) -> str:
    return _TEMPLATE.sub("{}", path.split("?", 1)[0]).rstrip("/") or "/"


def reference_errors(ir: dict[str, Any], route_keys: set[str], page_shapes: set[str]) -> list[str]:
    """Model-proposed workflows may only use endpoints and pages that discovery found."""
    errors = []
    api_steps = [*ir.get("steps", []), *ir.get("api_equivalent", [])]
    api_steps += [p["api"] for p in ir.get("preconditions", []) if p.get("api")]
    for step in api_steps:
        req = step.get("request")
        if req and f"{req['method']} {path_shape(req['path'])}" not in route_keys:
            errors.append(f"step {step.get('id')}: {req['method']} {req['path']} is not in the route table")
    for step in ir.get("steps", []):
        if step.get("action") == "navigate" and page_shapes and path_shape(step.get("url", "")) not in page_shapes:
            errors.append(f"step {step.get('id')}: page {step.get('url')} is not a known page")
    return errors


def _excerpts(snapshot: CodeSnapshot, routes: list[dict[str, Any]]) -> str:
    files = sorted({r["handler_file"] for r in routes}, key=lambda f: -sum(1 for r in routes if r["handler_file"] == f))
    parts, total = [], 0
    for path in files:
        text = snapshot.read(path)[:MAX_FILE_EXCERPT]
        if not text or total + len(text) > MAX_EXCERPT_CHARS:
            continue
        parts.append(wrap("source_file", path, text, MAX_FILE_EXCERPT))
        total += len(text)
    return "\n".join(parts)


def build(deps: DiscoveryDeps) -> StateGraph:
    cache: dict[str, Any] = {}

    async def _snapshot() -> CodeSnapshot:
        if "snapshot" not in cache:
            cache["sha"], cache["snapshot"] = await deps.ports.snapshot()
        snap: CodeSnapshot = cache["snapshot"]
        return snap

    async def snapshot(state: DiscoveryState) -> DiscoveryState:
        snap = await _snapshot()
        await deps.ports.progress("snapshot", {"files": len(snap.files), "excluded": len(snap.excluded)})
        return {"commit_sha": cache["sha"], "excluded_files": len(snap.excluded)}

    async def detect_stack(state: DiscoveryState) -> DiscoveryState:
        detected = stack.detect(await _snapshot()).to_dict()
        await deps.ports.progress("detect_stack", {"frameworks": detected["frameworks"]})
        return {"stack": detected}

    async def extract_routes(state: DiscoveryState) -> DiscoveryState:
        st = stack.Stack(apps=[stack.App(**a) for a in state["stack"]["apps"]])
        found = routes_mod.extract(await _snapshot(), st)
        await deps.ports.progress(
            "extract_routes",
            {"api": sum(r.kind == "api" for r in found), "pages": sum(r.kind == "page" for r in found)},
        )
        return {"routes": [r.to_dict() for r in found]}

    async def import_openapi(state: DiscoveryState) -> DiscoveryState:
        ops: list[dict[str, Any]] = []
        errors: list[str] = []
        for text in await deps.ports.openapi_documents():
            try:
                ops += [asdict(o) for o in openapi_mod.operations(openapi_mod.load(text))]
            except openapi_mod.OpenAPIError as exc:
                errors.append(str(exc))
        if ops or errors:
            await deps.ports.progress("import_openapi", {"operations": len(ops), "errors": errors})
        return {"operations": ops, "openapi_errors": errors}

    def _routes(state: DiscoveryState) -> list[routes_mod.Route]:
        return [routes_mod.Route(**r) for r in state.get("routes", [])]

    async def synthesize_baseline(state: DiscoveryState) -> DiscoveryState:
        ops = [openapi_mod.Operation(**o) for o in state.get("operations", [])]
        candidates = synthesis.synthesize(_routes(state), ops, await _snapshot(), await deps.ports.secret_names())
        await deps.ports.progress("synthesize", {"baseline": len(candidates)})
        return {
            "candidates": [
                {
                    "ir": c.ir,
                    "confidence": c.confidence,
                    "data_write": c.data_write,
                    "source": c.source,
                    "origin": "rules",
                }
                for c in candidates
            ]
        }

    async def synthesize_llm(state: DiscoveryState) -> DiscoveryState:
        llm: dict[str, Any] = {"prompt": prompts.SYNTHESIZE_VERSION, "accepted": 0, "rejected": [], "errors": []}
        if not deps.llm_enabled or state.get("budget_exhausted"):
            return {"llm": {**llm, "skipped": True}}
        routes = state.get("routes", [])
        route_keys = {f"{r['method']} {path_shape(r['path'])}" for r in routes if r["kind"] == "api"}
        route_keys |= {f"{o['method']} {path_shape(o['path'])}" for o in state.get("operations", [])}
        page_shapes = {path_shape(r["path"]) for r in routes if r["kind"] == "page"}
        existing = {c["ir"]["key"]: c for c in state.get("candidates", [])}
        table = [
            {k: r[k] for k in ("method", "path", "kind", "auth_hint", "body_fields", "handler_file", "line")}
            for r in routes
        ] + [
            {"method": o["method"], "path": o["path"], "kind": "api", "openapi": o["operation_id"]}
            for o in state.get("operations", [])
        ]
        summary = [
            {
                "key": k,
                "title": c["ir"]["title"],
                "kind": c["ir"]["kind"],
                "steps": [
                    s["request"]["method"] + " " + s["request"]["path"] if "request" in s else s.get("action")
                    for s in c["ir"].get("steps", [])
                ],
            }
            for k, c in existing.items()
        ]
        content = (
            f"Stack: {payload(state.get('stack', {}).get('frameworks', []))}\n"
            f"Route table:\n{payload(table)}\n"
            f"Existing workflows:\n{payload(summary)}\n"
            f"Source excerpts:\n{_excerpts(await _snapshot(), routes)}"
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        accepted: dict[str, dict[str, Any]] = {}
        exhausted = False
        for _attempt in range(2):
            try:
                out = await deps.gateway.structured(
                    LLMRequest(
                        "discovery.synthesize",
                        prompts.SYNTHESIZE_SYSTEM,
                        messages,
                        prompts.SYNTHESIS_OUTPUT,
                        constrained=False,
                    ),
                    org_id=deps.org_id,
                    subject_id=deps.run_id,
                    budget=deps.budget,
                    sink=deps.sink,
                )
            except BudgetExhausted:
                exhausted = True
                break
            except (LLMUnavailable, LLMRefusal, SchemaInvalid) as exc:
                llm["errors"].append(f"{type(exc).__name__}: {exc}"[:300])
                break
            problems: list[str] = []
            for ir in out.get("workflows", []):
                if not isinstance(ir, dict):
                    continue
                errs = validation_errors(ir) if "key" in ir and "kind" in ir else ["missing key or kind"]
                errs = errs or reference_errors(ir, route_keys, page_shapes)
                if errs:
                    problems.append(f"{ir.get('key', '?')}: " + "; ".join(errs[:3]))
                    llm["rejected"].append({"key": ir.get("key"), "errors": errs[:5]})
                    continue
                accepted[ir["key"]] = ir
            if not problems:
                break
            messages = [
                *messages,
                {"role": "assistant", "content": json.dumps(out)[:20000]},
                {
                    "role": "user",
                    "content": "These workflows were rejected:\n- "
                    + "\n- ".join(problems[:15])
                    + "\nReturn corrected versions of the rejected workflows only (same JSON shape).",
                },
            ]
        candidates = list(state.get("candidates", []))
        for key, ir in accepted.items():
            ir.setdefault("source", {"type": "static", "evidence": []})
            entry = {
                "ir": ir,
                "confidence": 0.7,
                "data_write": any(s.get("request", {}).get("method", "GET") != "GET" for s in ir.get("steps", [])),
                "source": "static",
                "origin": "model",
            }
            if key in existing:
                candidates = [entry if c["ir"]["key"] == key else c for c in candidates]
            else:
                candidates.append(entry)
        llm["accepted"] = len(accepted)
        await deps.ports.progress("synthesize_llm", {"accepted": len(accepted), "rejected": len(llm["rejected"])})
        return {"candidates": candidates, "llm": llm, "budget_exhausted": exhausted}

    async def rank_criticality(state: DiscoveryState) -> DiscoveryState:
        candidates = [dict(c) for c in state.get("candidates", [])]
        proposed: dict[str, str] = {}
        llm = dict(state.get("llm", {}))
        if deps.llm_enabled and candidates and not state.get("budget_exhausted"):
            items = [
                {
                    "key": c["ir"]["key"],
                    "title": c["ir"]["title"],
                    "steps": [
                        s["request"]["method"] + " " + s["request"]["path"]
                        if "request" in s
                        else f"{s['action']} {s.get('url', '')}"
                        for s in c["ir"].get("steps", [])
                    ][:12],
                }
                for c in candidates
            ]
            try:
                out = await deps.gateway.structured(
                    LLMRequest(
                        "discovery.criticality",
                        prompts.CRITICALITY_SYSTEM,
                        [{"role": "user", "content": f"Workflows:\n{payload({'workflows': items})}"}],
                        prompts.CRITICALITY_OUTPUT,
                    ),
                    org_id=deps.org_id,
                    subject_id=deps.run_id,
                    budget=deps.budget,
                    sink=deps.sink,
                )
                proposed = {a["key"]: a["criticality"] for a in out["assessments"]}
            except BudgetExhausted:
                state = {**state, "budget_exhausted": True}
            except (LLMUnavailable, LLMRefusal, SchemaInvalid) as exc:
                llm.setdefault("errors", []).append(f"criticality {type(exc).__name__}"[:300])
        for c in candidates:
            ir = dict(c["ir"])
            ir["criticality"] = criticality.apply(ir, proposed.get(ir["key"]), c["data_write"])
            c["ir"] = ir
        return {"candidates": candidates, "llm": llm, "budget_exhausted": bool(state.get("budget_exhausted"))}

    async def persist(state: DiscoveryState) -> DiscoveryState:
        result = await deps.ports.persist(state.get("candidates", []), state.get("stack", {}))
        await deps.ports.progress("persist", result)
        return {"persisted": result}

    graph = StateGraph(DiscoveryState)
    steps = [
        snapshot,
        detect_stack,
        extract_routes,
        import_openapi,
        synthesize_baseline,
        synthesize_llm,
        rank_criticality,
        persist,
    ]
    for fn in steps:
        graph.add_node(fn.__name__, fn)
    graph.add_edge(START, steps[0].__name__)
    for a, b in itertools.pairwise(steps):
        graph.add_edge(a.__name__, b.__name__)
    graph.add_edge(steps[-1].__name__, END)
    return graph


async def run(deps: DiscoveryDeps, thread_id: str) -> DiscoveryState:
    """Runs (or resumes after a crash) the discovery thread to completion."""
    async with checkpointer() as saver:
        app = build(deps).compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
        current = await app.aget_state(config)
        start: DiscoveryState | None = None if current.next else {}
        result = await app.ainvoke(start, config)
        return cast(DiscoveryState, result)
