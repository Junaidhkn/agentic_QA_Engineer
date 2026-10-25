"""Repair graph (agent-workflows §6, ADR-0007).

load_context → plan_fix → policy_check_plan → write_regression_test →
start_base_check → await_base_check (test must fail 3/3 on base) → implement_fix →
start_local_check → await_local_check (regression + existing tests on the patch, ≤ 5 rounds) →
export_candidate → finish

The agent proposes; deterministic nodes decide. Checks run as ``repair_check`` sandbox jobs:
the start node enqueues the job and the following node waits on a LangGraph ``interrupt``
(side effects never sit in the interrupted node, which LangGraph re-executes on resume). The
graph ends at the candidate: verification, approval and the pull request belong to separate
deterministic modules the agent cannot reach.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.checkpoint import checkpointer
from reliability.agents.common.untrusted import payload, wrap
from reliability.agents.discovery.snapshot import CodeSnapshot
from reliability.agents.repair import prompts
from reliability.agents.repair.commands import Commands, detect
from reliability.agents.repair.tools import Phase, build_tools
from reliability.agents.repair.workspace import Workspace
from reliability.llm import BudgetExhausted, Gateway, LLMRefusal, LLMRequest, LLMUnavailable, SchemaInvalid
from reliability.llm.types import UsageSink

MAX_PLAN_TRIES = 3
MAX_TEST_TRIES = 3
MAX_ITERATIONS = 5
MAX_TOOL_CALLS = 40
BASE_RUNS = 3
EXCERPT_LINES = 200


class RepairPorts(Protocol):
    async def context(self) -> dict[str, Any]: ...
    async def snapshot(self) -> CodeSnapshot: ...
    def path_policy(self, path: str) -> str | None: ...
    def diff_policy(self, changes: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]: ...
    async def start_checks(
        self, stage: str, files: dict[str, str | None], checks: list[dict[str, Any]]
    ) -> dict[str, Any] | None: ...
    async def evidence(self, evidence_id: str) -> str | None: ...
    async def progress(self, status: str) -> None: ...
    async def finish(self, result: dict[str, Any]) -> None: ...


@dataclass
class RepairDeps:
    ports: RepairPorts
    gateway: Gateway
    sink: UsageSink
    budget: GraphBudget
    org_id: uuid.UUID
    attempt_id: uuid.UUID
    llm_enabled: bool = True


class RepairState(TypedDict, total=False):
    context: dict[str, Any]
    plan: dict[str, Any]
    overlay: dict[str, str | None]
    test_paths: list[str]
    plan_tries: int
    test_tries: int
    iterations: int
    feedback: str
    pending: dict[str, Any] | None
    check: dict[str, Any] | None
    explanation: str
    result: dict[str, Any]
    model_info: dict[str, Any]
    budget_exhausted: bool


def _fail(reason: str, status: str = "failed", **extra: Any) -> RepairState:
    return {"result": {"status": status, "reason": reason, **extra}}


def check_summary(check: dict[str, Any] | None, check_id: str) -> list[int | None]:
    return [a.get("exit_code") for a in ((check or {}).get("checks") or {}).get(check_id, [])]


def _output(check: dict[str, Any] | None, check_id: str, limit: int = 4000) -> str:
    attempts = ((check or {}).get("checks") or {}).get(check_id) or []
    return (attempts[-1].get("output") or "")[-limit:] if attempts else ""


def build(deps: RepairDeps) -> StateGraph:
    ports = deps.ports

    async def _workspace(state: RepairState) -> tuple[Workspace, Commands | None]:
        snapshot = await ports.snapshot()
        root = (state.get("context") or {}).get("plan_root", "")
        return Workspace(snapshot, dict(state.get("overlay") or {})), detect(snapshot, root)

    async def _llm(request: LLMRequest, state: RepairState) -> Any:
        if not deps.llm_enabled or state.get("budget_exhausted"):
            return None
        try:
            if request.schema is not None:
                return await deps.gateway.structured(
                    request, org_id=deps.org_id, subject_id=deps.attempt_id, budget=deps.budget, sink=deps.sink
                )
            return await deps.gateway.complete(
                request, org_id=deps.org_id, subject_id=deps.attempt_id, budget=deps.budget, sink=deps.sink
            )
        except BudgetExhausted:
            state["budget_exhausted"] = True
        except (LLMUnavailable, LLMRefusal, SchemaInvalid) as exc:
            state.setdefault("model_info", {}).setdefault("errors", []).append(
                f"{request.purpose}: {type(exc).__name__}"
            )
        return None

    def _brief(state: RepairState) -> str:
        ctx = state.get("context") or {}
        return (
            f"Finding:\n{payload(ctx.get('finding'))}\nHypothesis:\n{payload(ctx.get('hypothesis'))}\n"
            f"Evidence:\n{payload(ctx.get('evidence'))}\nConstraints:\n{payload(ctx.get('constraints'))}\n"
            f"Plan:\n{payload(state.get('plan'))}\n"
        )

    async def _tool_loop(state: RepairState, phase: Phase, system: str, ask: str) -> str | None:
        ws, commands = await _workspace(state)
        assert commands is not None
        registry = build_tools(
            deps.budget,
            ws,
            phase,
            commands,
            ports.path_policy,
            set(state.get("test_paths") or []),
            ports.evidence,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": ask}]
        text, calls = None, 0
        while calls < MAX_TOOL_CALLS:
            resp = await _llm(LLMRequest("repair.code", system, messages, tools=registry.specs()), state)
            if resp is None:
                break
            if not resp.tool_calls:
                text = resp.text[:4000]
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for call in resp.tool_calls:
                calls += 1
                try:
                    out = await registry.call(call["name"], call.get("input") or {})
                except BudgetExhausted:
                    out, calls = "error: tool budget exhausted", MAX_TOOL_CALLS
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
            messages.append({"role": "user", "content": results})
        state["overlay"] = ws.overlay
        info = state.setdefault("model_info", {})
        info["tool_calls"] = int(info.get("tool_calls", 0)) + calls
        return text

    # ------------------------------------------------------------------ nodes ----
    async def load_context(state: RepairState) -> RepairState:
        await ports.progress("planning")
        ctx = await ports.context()
        snapshot = await ports.snapshot()
        if detect(snapshot, ctx.get("plan_root", "")) is None:
            return {**_fail("no supported test runner found (pytest, jest, vitest or node --test)"), "context": ctx}
        excerpts = []
        for loc in (ctx.get("hypothesis") or {}).get("code_locations") or []:
            text = snapshot.files.get(str(loc.get("path")))
            if text:
                excerpts.append(wrap("file", str(loc["path"]), "\n".join(text.splitlines()[:EXCERPT_LINES])))
        return {
            "context": {**ctx, "excerpts": excerpts[:5]},
            "overlay": {},
            "test_paths": [],
            "plan_tries": 0,
            "test_tries": 0,
            "iterations": 0,
            "feedback": ctx.get("feedback") or "",
            "model_info": {
                "prompts": [prompts.PLAN_VERSION, prompts.TEST_VERSION, prompts.CODE_VERSION],
                "llm_enabled": deps.llm_enabled,
            },
        }

    async def plan_fix(state: RepairState) -> RepairState:
        ctx = state.get("context") or {}
        out = await _llm(
            LLMRequest(
                "repair.plan",
                prompts.PLAN_SYSTEM,
                [
                    {
                        "role": "user",
                        "content": _brief(state)
                        + "\n".join(ctx.get("excerpts") or [])
                        + (f"\nFeedback:\n{payload(state.get('feedback'))}" if state.get("feedback") else ""),
                    }
                ],
                prompts.PLAN_OUTPUT,
            ),
            state,
        )
        tries = int(state.get("plan_tries", 0)) + 1
        if not out or not out.get("files"):
            reason = (
                "token budget exhausted"
                if state.get("budget_exhausted")
                else "the model proposed no change (a configured model is required to write fixes)"
            )
            return {
                **_fail(reason, "budget_exhausted" if state.get("budget_exhausted") else "failed"),
                "model_info": state.get("model_info", {}),
            }
        return {"plan": out, "plan_tries": tries, "model_info": state.get("model_info", {})}

    async def policy_check_plan(state: RepairState) -> RepairState:
        violations = [v for f in (state.get("plan") or {}).get("files", []) if (v := ports.path_policy(str(f)))]
        if not violations:
            return {"feedback": state.get("feedback", "")}
        if int(state.get("plan_tries", 0)) >= MAX_PLAN_TRIES:
            return _fail("the plan kept touching files outside policy: " + "; ".join(violations[:3]))
        return {"feedback": "The plan violates the change policy: " + "; ".join(violations), "plan": {}}

    async def write_regression_test(state: RepairState) -> RepairState:
        await ports.progress("coding")
        # The task payload comes first; the brief follows as context.
        ask = payload({"phase": "test", "feedback": state.get("feedback", "")}) + "\n" + _brief(state)
        await _tool_loop(state, "test", prompts.TEST_SYSTEM, ask)
        ws, commands = await _workspace(state)
        assert commands is not None
        tests = sorted(p for p in ws.changes() if commands.is_test_path(p))
        tries = int(state.get("test_tries", 0)) + 1
        if not tests:
            return {
                **(_fail("the model wrote no regression test") if tries >= MAX_TEST_TRIES else {}),
                "test_tries": tries,
                "overlay": ws.overlay,
                "feedback": "No test file was written. Write one regression test file.",
                "model_info": state.get("model_info", {}),
            }
        return {
            "test_tries": tries,
            "test_paths": tests,
            "overlay": ws.overlay,
            "model_info": state.get("model_info", {}),
        }

    async def start_base_check(state: RepairState) -> RepairState:
        ws, commands = await _workspace(state)
        assert commands is not None
        tests = {p: ws.read(p) for p in state.get("test_paths") or []}
        pending = await ports.start_checks(
            "base", tests, [{"id": "regression", "argv": commands.regression(list(tests)), "repeat": BASE_RUNS}]
        )
        if pending is None:
            return _fail("sandbox checks are unavailable for this project")
        return {"pending": pending}

    async def await_base_check(state: RepairState) -> RepairState:
        outcome = interrupt({"waiting_for": "check", "stage": "base", **(state.get("pending") or {})})
        return {"check": outcome, "pending": None}

    async def implement_fix(state: RepairState) -> RepairState:
        ask = (
            payload(
                {
                    "phase": "code",
                    "regression_test": state.get("test_paths"),
                    "iteration": int(state.get("iterations", 0)) + 1,
                    "feedback": state.get("feedback", ""),
                }
            )
            + "\n"
            + _brief(state)
        )
        explanation = await _tool_loop(state, "code", prompts.CODE_SYSTEM, ask)
        return {
            "overlay": state.get("overlay", {}),
            "explanation": explanation or state.get("explanation", ""),
            "iterations": int(state.get("iterations", 0)) + 1,
            "model_info": state.get("model_info", {}),
        }

    async def start_local_check(state: RepairState) -> RepairState:
        ws, commands = await _workspace(state)
        assert commands is not None
        changes = ws.changes()
        tests = list(state.get("test_paths") or [])
        if not [p for p in changes if p not in tests]:
            return {"check": {"built": True, "checks": {}, "no_change": True}, "pending": None}
        checks = [{"id": "regression", "argv": commands.regression(tests), "repeat": 1}]
        if (existing := commands.existing_without(tests)) is not None:
            checks.append({"id": "existing_tests", "argv": existing, "repeat": 1})
        pending = await ports.start_checks("local", {p: new for p, (_, new) in changes.items()}, checks)
        if pending is None:
            return _fail("sandbox checks are unavailable for this project")
        return {"pending": pending, "check": None}

    async def await_local_check(state: RepairState) -> RepairState:
        if state.get("pending") is None:
            return {}
        outcome = interrupt({"waiting_for": "check", "stage": "local", **(state.get("pending") or {})})
        return {"check": outcome, "pending": None}

    async def export_candidate(state: RepairState) -> RepairState:
        ws, _ = await _workspace(state)
        changes = ws.changes()
        verdict = ports.diff_policy(changes)
        if not verdict.get("passed"):
            if int(state.get("iterations", 0)) < MAX_ITERATIONS:
                return {"feedback": "The change violates the diff policy: " + "; ".join(verdict.get("violations", []))}
            return _fail("the change violates the diff policy: " + "; ".join(verdict.get("violations", [])[:3]))
        tests = set(state.get("test_paths") or [])
        return {
            "result": {
                "status": "candidate",
                "changes": {p: list(v) for p, v in changes.items() if p not in tests},
                "test_changes": {p: list(v) for p, v in changes.items() if p in tests},
                "test_paths": sorted(tests),
                "explanation": state.get("explanation", ""),
                "plan": state.get("plan"),
                "stats": verdict.get("stats"),
                "iterations": state.get("iterations", 0),
            }
        }

    async def finish(state: RepairState) -> RepairState:
        result = {
            **(state.get("result") or {"status": "failed", "reason": "no result"}),
            "model_info": {**state.get("model_info", {}), "tokens": deps.budget.spent},
        }
        await ports.finish(result)
        return {"result": result}

    # ---------------------------------------------------------------- routing ----
    def done(state: RepairState) -> bool:
        return bool(state.get("result"))

    def after_load(state: RepairState) -> str:
        return "finish" if done(state) else "plan_fix"

    def after_plan(state: RepairState) -> str:
        return "finish" if done(state) else "policy_check_plan"

    def after_policy(state: RepairState) -> str:
        if done(state):
            return "finish"
        return "write_regression_test" if state.get("plan") else "plan_fix"

    def after_test(state: RepairState) -> str:
        if done(state):
            return "finish"
        return "start_base_check" if state.get("test_paths") else "write_regression_test"

    def after_base_start(state: RepairState) -> str:
        return "finish" if done(state) else "await_base_check"

    def after_base(state: RepairState) -> Literal["implement_fix", "write_regression_test", "base_failed"]:
        check = state.get("check") or {}
        codes = check_summary(check, "regression")
        if not check.get("built"):
            return "base_failed"
        if len(codes) == BASE_RUNS and all(c not in (0, None) for c in codes):
            return "implement_fix"
        return "write_regression_test" if int(state.get("test_tries", 0)) < MAX_TEST_TRIES else "base_failed"

    async def base_failed(state: RepairState) -> RepairState:
        check = state.get("check") or {}
        if not check.get("built"):
            return _fail(f"the sandbox could not be built for the base commit: {check.get('error')}")
        codes = check_summary(check, "regression")
        return _fail(f"the regression test did not fail on the base commit 3/3 (exit codes {codes})")

    async def test_feedback(state: RepairState) -> RepairState:
        check = state.get("check") or {}
        stale = set(state.get("test_paths") or [])
        return {
            "feedback": (
                f"The regression test passed on the unmodified code (exit codes "
                f"{check_summary(check, 'regression')}); it must fail because of the defect.\n"
                + _output(check, "regression")
            ),
            "test_paths": [],
            "overlay": {p: v for p, v in (state.get("overlay") or {}).items() if p not in stale},
        }

    def after_local_start(state: RepairState) -> str:
        return "finish" if done(state) else "await_local_check"

    def after_local(state: RepairState) -> str:
        check = state.get("check") or {}
        ok = (
            check.get("built")
            and not check.get("no_change")
            and check_summary(check, "regression") == [0]
            and all(c == 0 for c in check_summary(check, "existing_tests"))
        )
        if ok:
            return "export_candidate"
        return "local_feedback"

    async def local_feedback(state: RepairState) -> RepairState:
        if int(state.get("iterations", 0)) >= MAX_ITERATIONS:
            return _fail(f"local checks still failing after {MAX_ITERATIONS} iterations")
        check = state.get("check") or {}
        if check.get("no_change"):
            msg = "No source file was changed yet. Implement the fix."
        elif not check.get("built"):
            msg = f"The patched application failed to build or start: {check.get('error')}"
        else:
            msg = (
                f"Regression test exit codes {check_summary(check, 'regression')}, existing tests "
                f"{check_summary(check, 'existing_tests')}.\nRegression output:\n{_output(check, 'regression')}\n"
                f"Existing tests output:\n{_output(check, 'existing_tests', 2000)}"
            )
        return {"feedback": msg}

    def after_local_feedback(state: RepairState) -> str:
        return "finish" if done(state) else "implement_fix"

    def after_export(state: RepairState) -> str:
        return "finish" if done(state) else "implement_fix"

    g = StateGraph(RepairState)
    for fn in (
        load_context,
        plan_fix,
        policy_check_plan,
        write_regression_test,
        start_base_check,
        await_base_check,
        base_failed,
        test_feedback,
        implement_fix,
        start_local_check,
        await_local_check,
        local_feedback,
        export_candidate,
        finish,
    ):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "load_context")
    g.add_conditional_edges("load_context", after_load, ["plan_fix", "finish"])
    g.add_conditional_edges("plan_fix", after_plan, ["policy_check_plan", "finish"])
    g.add_conditional_edges("policy_check_plan", after_policy, ["write_regression_test", "plan_fix", "finish"])
    g.add_conditional_edges(
        "write_regression_test", after_test, ["start_base_check", "write_regression_test", "finish"]
    )
    g.add_conditional_edges("start_base_check", after_base_start, ["await_base_check", "finish"])
    g.add_conditional_edges(
        "await_base_check",
        after_base,
        {"implement_fix": "implement_fix", "write_regression_test": "test_feedback", "base_failed": "base_failed"},
    )
    g.add_edge("test_feedback", "write_regression_test")
    g.add_edge("base_failed", "finish")
    g.add_edge("implement_fix", "start_local_check")
    g.add_conditional_edges("start_local_check", after_local_start, ["await_local_check", "finish"])
    g.add_conditional_edges("await_local_check", after_local, ["export_candidate", "local_feedback"])
    g.add_conditional_edges("local_feedback", after_local_feedback, ["implement_fix", "finish"])
    g.add_conditional_edges("export_candidate", after_export, ["finish", "implement_fix"])
    g.add_edge("finish", END)
    return g


async def start(deps: RepairDeps, thread_id: str) -> dict[str, Any]:
    return await _invoke(deps, thread_id, {})


async def resume(deps: RepairDeps, thread_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
    return await _invoke(deps, thread_id, Command(resume=outcome))


async def _invoke(deps: RepairDeps, thread_id: str, value: Any) -> dict[str, Any]:
    async with checkpointer() as saver:
        app = build(deps).compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 120}
        out = await app.ainvoke(value, config)
        pending = out.get("__interrupt__") if isinstance(out, dict) else None
        if pending:
            return {"waiting": pending[0].value}
        return dict(json.loads(json.dumps((out or {}).get("result") or {}, default=str)))
