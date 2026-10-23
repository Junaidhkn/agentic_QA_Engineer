"""Test-generation graph (agent-workflows §4, ADR-0013 deviation recorded in plan.md).

compile → start_validation → [interrupt: runner dry-run ×3] → classify → repair? → compile …
→ finish

Playwright and k6 code is compiled deterministically from Workflow IR; the model's job is to
repair the IR of a workflow whose dry run failed (at most 3 attempts, FR-12). Validation runs
on a runner as a ``test_validation`` job against a verified target (1 VU, 1 iteration, repeated
3× to detect flakiness); the graph waits for it with ``interrupt`` and is resumed by the job's
completion handler. Outcome per test case: ``valid`` (3/3 passed), ``flaky`` (mixed),
``invalid`` (still failing after 3 repairs, excluded from runs).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.checkpoint import checkpointer
from reliability.agents.common.untrusted import payload, wrap
from reliability.agents.discovery.graph import path_shape
from reliability.agents.testgen import prompts
from reliability.llm import BudgetExhausted, Gateway, LLMRefusal, LLMRequest, LLMUnavailable, SchemaInvalid
from reliability.llm.types import UsageSink
from reliability.modules.workflows.ir import validation_errors

MAX_REPAIRS = 3
REPEATS = 3


class TestgenPorts(Protocol):
    """Implemented by ``modules/testsuites``."""

    async def compile(self, irs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Write the suite bundle for these IRs; returns cases ``{id, workflow_key, type}``."""
        ...

    async def start_validation(self, case_ids: list[str], repeats: int) -> str | None:
        """Enqueue the dry-run job; ``None`` when no verified target is available."""
        ...

    async def finish(self, cases: dict[str, dict[str, Any]], status: str, info: dict[str, Any]) -> None: ...


@dataclass
class TestgenDeps:
    ports: TestgenPorts
    gateway: Gateway
    sink: UsageSink
    budget: GraphBudget
    org_id: uuid.UUID
    suite_id: uuid.UUID
    llm_enabled: bool = True


class TestgenState(TypedDict, total=False):
    irs: dict[str, dict[str, Any]]  # workflow key -> IR compiled into the suite
    original: dict[str, dict[str, Any]]
    attempts: dict[str, int]  # workflow key -> repair attempts used
    cases: dict[str, dict[str, Any]]  # case id -> {workflow_key, type, status, detail}
    pending: list[str]  # case ids awaiting validation
    validation_job: str | None
    results: dict[str, list[dict[str, Any]]]
    repaired: dict[str, str]  # key -> rationale
    status: str


def classify_outcomes(outcomes: list[dict[str, Any]]) -> str:
    passed = sum(1 for o in outcomes if o.get("exit_code") == 0)
    if outcomes and passed == len(outcomes):
        return "valid"
    if passed:
        return "flaky"
    return "failed"


def endpoint_set(irs: dict[str, dict[str, Any]]) -> set[str]:
    out = set()
    for ir in irs.values():
        for s in [*ir.get("steps", []), *ir.get("api_equivalent", [])]:
            if "request" in s:
                out.add(f"{s['request']['method']} {path_shape(s['request']['path'])}")
    return out


def build(deps: TestgenDeps) -> StateGraph:
    async def compile_suite(state: TestgenState) -> TestgenState:
        cases = await deps.ports.compile(state["irs"])
        previous = state.get("cases", {})
        by_workflow_type = {(c["workflow_key"], c["type"]): c for c in previous.values()}
        merged: dict[str, dict[str, Any]] = {}
        pending = []
        for case in cases:
            prior = by_workflow_type.get((case["workflow_key"], case["type"]))
            if not case.get("validatable", True):
                merged[case["id"]] = {**case, "status": "pending", "detail": {"reason": "not dry-run"}}
                continue
            keep = prior is not None and prior.get("status") in ("valid", "flaky", "invalid")
            entry = {
                **case,
                "status": prior["status"] if keep and prior else "pending",
                "detail": prior.get("detail") if keep and prior else None,
            }
            merged[case["id"]] = entry
            if entry["status"] == "pending":
                pending.append(case["id"])
        return {"cases": merged, "pending": pending}

    async def validate(state: TestgenState) -> TestgenState:
        # Side effect in its own node: on resume LangGraph re-executes only the interrupted node.
        if not state.get("pending"):
            return {"validation_job": None, "results": {}}
        job = await deps.ports.start_validation(state["pending"], REPEATS)
        return {"validation_job": job, "results": {}}

    async def await_validation(state: TestgenState) -> TestgenState:
        if not state.get("validation_job"):
            return {}
        results = interrupt({"waiting_for": "test_validation", "job_id": state["validation_job"]})
        return {"results": results or {}}

    async def classify(state: TestgenState) -> TestgenState:
        cases = {k: dict(v) for k, v in state["cases"].items()}
        if state.get("validation_job") is None and state.get("pending"):
            for cid in state["pending"]:
                cases[cid].update(status="pending", detail={"reason": "no verified target to dry-run against"})
            return {"cases": cases}
        for cid in state.get("pending", []):
            outcomes = state.get("results", {}).get(cid, [])
            verdict = classify_outcomes(outcomes)
            failure = next((o.get("log_tail") for o in outcomes if o.get("exit_code")), None)
            cases[cid].update(
                status=verdict,
                detail={
                    "runs": len(outcomes),
                    "passed": sum(1 for o in outcomes if o.get("exit_code") == 0),
                    "failure": (failure or "")[-2000:] or None,
                },
            )
        return {"cases": cases}

    def needs_repair(state: TestgenState) -> str:
        attempts = state.get("attempts", {})
        failed = {c["workflow_key"] for c in state["cases"].values() if c["status"] == "failed"}
        if deps.llm_enabled and any(attempts.get(k, 0) < MAX_REPAIRS for k in failed):
            return "repair"
        return "finish"

    async def repair(state: TestgenState) -> TestgenState:
        irs = dict(state["irs"])
        attempts = dict(state.get("attempts", {}))
        repaired = dict(state.get("repaired", {}))
        cases = {k: dict(v) for k, v in state["cases"].items()}
        known = endpoint_set(state.get("original", irs)) | endpoint_set(irs)
        failed: dict[str, list[dict[str, Any]]] = {}
        for c in cases.values():
            if c["status"] == "failed" and attempts.get(c["workflow_key"], 0) < MAX_REPAIRS:
                failed.setdefault(c["workflow_key"], []).append(c)
        for key, wf_cases in failed.items():
            attempts[key] = attempts.get(key, 0) + 1
            failures = "\n".join(
                wrap(
                    "dry_run_output",
                    f"{c['type']}:{key}",
                    (c.get("detail") or {}).get("failure") or "(no output)",
                    4000,
                )
                for c in wf_cases
            )
            message = (
                f"Workflow IR:\n{payload({'ir': irs[key]})}\nKnown endpoints:\n{payload(sorted(known))}\n"
                f"Dry-run failures:\n{failures}"
            )
            try:
                out = await deps.gateway.structured(
                    LLMRequest(
                        "testgen.repair",
                        prompts.REPAIR_SYSTEM,
                        [{"role": "user", "content": message}],
                        prompts.REPAIR_OUTPUT,
                        constrained=False,
                    ),
                    org_id=deps.org_id,
                    subject_id=deps.suite_id,
                    budget=deps.budget,
                    sink=deps.sink,
                )
            except BudgetExhausted:
                attempts[key] = MAX_REPAIRS
                for c in wf_cases:
                    c["detail"] = {**(c.get("detail") or {}), "reason": "budget"}
                continue
            except (LLMUnavailable, LLMRefusal, SchemaInvalid) as exc:
                for c in wf_cases:
                    c["detail"] = {**(c.get("detail") or {}), "repair_error": type(exc).__name__}
                continue
            candidate = out.get("ir") if isinstance(out.get("ir"), dict) else {}
            problems = validation_errors(candidate) if candidate.get("key") == key else ["key changed"]
            if not problems and candidate.get("kind") != irs[key].get("kind"):
                problems = ["kind changed"]
            if not problems and irs[key].get("destructive") and not candidate.get("destructive"):
                problems = ["destructive flag removed"]
            unknown = endpoint_set({key: candidate}) - known if not problems else set()
            if unknown:
                problems = [f"unknown endpoints {sorted(unknown)}"]
            if problems or json.dumps(candidate, sort_keys=True) == json.dumps(irs[key], sort_keys=True):
                attempts[key] = MAX_REPAIRS  # no usable repair: stop retrying this workflow
                for c in wf_cases:
                    c["detail"] = {**(c.get("detail") or {}), "repair_rejected": problems[:3] or ["unchanged"]}
                continue
            irs[key] = candidate
            repaired[key] = str(out.get("rationale", ""))[:2000]
            for c in wf_cases:
                c["status"] = "repairing"
        return {"irs": irs, "attempts": attempts, "repaired": repaired, "cases": cases}

    def after_repair(state: TestgenState) -> str:
        return "compile_suite" if any(c["status"] == "repairing" for c in state["cases"].values()) else "finish"

    async def finish(state: TestgenState) -> TestgenState:
        cases = {k: dict(v) for k, v in state["cases"].items()}
        for c in cases.values():
            if c["status"] == "failed":
                c["status"] = "invalid"
        statuses = {c["status"] for c in cases.values()}
        if statuses <= {"valid", "flaky", "pending"}:
            status = "ready"
        elif statuses & {"valid", "flaky", "pending"}:
            status = "partially_ready"
        else:
            status = "failed"
        info = {
            "repaired": state.get("repaired", {}),
            "repair_attempts": state.get("attempts", {}),
            "repaired_irs": {k: state["irs"][k] for k in state.get("repaired", {})},
            "prompt": prompts.REPAIR_VERSION,
            "tokens": deps.budget.spent,
        }
        await deps.ports.finish(cases, status, info)
        return {"cases": cases, "status": status}

    graph = StateGraph(TestgenState)
    for fn in (compile_suite, validate, await_validation, classify, repair, finish):
        graph.add_node(fn.__name__, fn)
    graph.add_edge(START, "compile_suite")
    graph.add_edge("compile_suite", "validate")
    graph.add_edge("validate", "await_validation")
    graph.add_edge("await_validation", "classify")
    graph.add_conditional_edges("classify", needs_repair, ["repair", "finish"])
    graph.add_conditional_edges("repair", after_repair, ["compile_suite", "finish"])
    graph.add_edge("finish", END)
    return graph


async def start(deps: TestgenDeps, thread_id: str, irs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Runs until the graph waits for validation (returns ``{"waiting": job_id}``) or finishes."""
    return await _invoke(deps, thread_id, {"irs": irs, "original": irs, "attempts": {}, "cases": {}})


async def resume(deps: TestgenDeps, thread_id: str, results: dict[str, Any] | None) -> dict[str, Any]:
    return await _invoke(deps, thread_id, Command(resume=results))


async def _invoke(deps: TestgenDeps, thread_id: str, value: Any) -> dict[str, Any]:
    async with checkpointer() as saver:
        app = build(deps).compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
        result = await app.ainvoke(value, config)
        pending = result.get("__interrupt__") if isinstance(result, dict) else None
        if pending:
            return {"waiting": pending[0].value}
        return {"status": result.get("status"), "cases": result.get("cases")}
