"""Diagnosis graph (agent-workflows §5, ADR-0015).

collect_context → triage → investigate → hypothesize → check_citations →
[start_experiment → await_experiment] → score → persist

Deterministic: the evidence pack, rule-based hypotheses, citation checks, the experiment and
its interpretation, the rubric and persistence. Model steps (tier S triage, tier L
investigation with read-only tools, tier L hypotheses) are optional: without a model, or when
it refuses/is unavailable/out of budget, the rule-based hypotheses still go through the same
checks. Hypotheses with no verified supporting citation are never kept; with none left the
diagnosis is ``inconclusive`` with next steps.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.checkpoint import checkpointer
from reliability.agents.common.untrusted import payload
from reliability.agents.diagnosis import evidence as pack_mod
from reliability.agents.diagnosis import prompts, rules
from reliability.agents.diagnosis.citations import Evidence, check_claim, check_code_location
from reliability.agents.diagnosis.code import CodeIndex
from reliability.agents.diagnosis.rubric import Factors, score
from reliability.agents.diagnosis.tools import Telemetry, build_tools
from reliability.llm import BudgetExhausted, Gateway, LLMRefusal, LLMRequest, LLMUnavailable, SchemaInvalid
from reliability.llm.types import UsageSink

MAX_TOOL_CALLS = 25
MAX_HYPOTHESES = 5
COST_CATEGORIES = {"missing_index", "n_plus_one", "unbounded_query", "blocking_io", "cpu_bound_handler"}
# Races are confirmed by the concurrency probe itself, not by a latency contrast.
CONTENTION_CATEGORIES = {"pool_exhaustion", "config_limit"}
DEFAULT_NEXT_STEPS = {
    "no_code": "Connect the repository (or upload the source) so handlers on the hot path can be inspected.",
    "no_db": "Run the test in a sandbox to capture database statistics for the run.",
    "no_traces": "Send OpenTelemetry traces from the app (project settings → telemetry) to see span breakdowns.",
}


class DiagnosisPorts(Protocol):
    async def finding(self) -> dict[str, Any]: ...
    async def existing_evidence(self) -> list[Evidence]: ...
    async def run_data(self) -> dict[str, Any]: ...
    async def code(self) -> CodeIndex | None: ...
    async def add_evidence(
        self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
    ) -> Evidence: ...
    async def progress(self, status: str) -> None: ...
    async def start_experiment(self, endpoint: str, vus: int) -> dict[str, Any] | None: ...
    async def persist(self, result: dict[str, Any]) -> None: ...


@dataclass
class DiagnosisDeps:
    ports: DiagnosisPorts
    gateway: Gateway
    sink: UsageSink
    budget: GraphBudget
    org_id: uuid.UUID
    diagnosis_id: uuid.UUID
    llm_enabled: bool = True
    code_enabled: bool = True
    experiments_enabled: bool = True
    telemetry: Telemetry | None = None


class DiagnosisState(TypedDict, total=False):
    evidence: list[dict[str, Any]]
    facts: dict[str, Any]
    endpoint: str | None
    handler_path: str | None
    has_code: bool
    has_db: bool
    triage: dict[str, Any]
    notes: str
    candidates: list[dict[str, Any]]
    checked: list[dict[str, Any]]
    next_steps: list[str]
    experiment: dict[str, Any] | None
    model_info: dict[str, Any]
    budget_exhausted: bool
    result: dict[str, Any]


def _ev(state: DiagnosisState) -> dict[str, Evidence]:
    return {e["id"]: Evidence(**e) for e in state.get("evidence", [])}


def interpret_experiment(category: str, exp: dict[str, Any]) -> dict[str, Any]:
    """Concurrency contrast: the same endpoint at 1 VU and at the observed load."""
    low, high = exp.get("low") or {}, exp.get("high") or {}
    lp, hp = float(low.get("p95_ms") or 0), float(high.get("p95_ms") or 0)
    le, he = float(low.get("error_rate") or 0), float(high.get("error_rate") or 0)
    load_dependent = (hp >= 2 * max(lp, 1) and hp - lp > 50) or he - le >= 0.05
    # A request that is already slow on its own (no contention) carries a per-request cost.
    present_at_one = lp >= max(25.0, 0.25 * hp)
    result = "inconclusive"
    if category in CONTENTION_CATEGORIES:
        result = "supports" if load_dependent else "contradicts"
    elif category in COST_CATEGORIES:
        result = "supports" if present_at_one else ("contradicts" if load_dependent and lp < 10 else "inconclusive")
    return {
        "name": "concurrency_contrast",
        "result": result,
        "low_p95_ms": lp,
        "high_p95_ms": hp,
        "low_error_rate": le,
        "high_error_rate": he,
        "vus": exp.get("vus"),
    }


def build(deps: DiagnosisDeps) -> StateGraph:
    created: list[Evidence] = []

    class Sink:
        async def add_evidence(
            self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
        ) -> Evidence:
            ev = await deps.ports.add_evidence(kind, locator, summary, snapshot)
            created.append(ev)
            return ev

    sink = Sink()

    async def _llm(request: LLMRequest, state: DiagnosisState) -> Any:
        """Model call with graceful degradation; returns None when the model can't help."""
        if not deps.llm_enabled or state.get("budget_exhausted"):
            return None
        try:
            if request.schema is not None:
                return await deps.gateway.structured(
                    request, org_id=deps.org_id, subject_id=deps.diagnosis_id, budget=deps.budget, sink=deps.sink
                )
            return await deps.gateway.complete(
                request, org_id=deps.org_id, subject_id=deps.diagnosis_id, budget=deps.budget, sink=deps.sink
            )
        except BudgetExhausted:
            state["budget_exhausted"] = True
        except (LLMUnavailable, LLMRefusal, SchemaInvalid) as exc:
            state.setdefault("model_info", {}).setdefault("errors", []).append(
                f"{request.purpose}: {type(exc).__name__}"
            )
        return None

    def _evidence_listing(state: DiagnosisState) -> list[dict[str, Any]]:
        return [
            {
                "id": e["id"],
                "kind": e["kind"],
                "summary": e["summary"][:300],
                "locator": {k: v for k, v in e["locator"].items() if k != "commit"},
            }
            for e in state.get("evidence", [])
        ][:80]

    async def collect_context(state: DiagnosisState) -> DiagnosisState:
        await deps.ports.progress("collecting")
        finding = await deps.ports.finding()
        run = await deps.ports.run_data()
        code = await deps.ports.code() if deps.code_enabled else None
        pack = await pack_mod.collect(sink, finding, await deps.ports.existing_evidence(), run, code)
        pack.facts["vus"] = (finding.get("headline") or {}).get("at_vus") or (run.get("profile") or {}).get("max_vus")
        return {
            "evidence": [asdict(e) for e in pack.items],
            "facts": pack.facts,
            "endpoint": pack.endpoint,
            "handler_path": pack.handler.path if pack.handler else None,
            "has_code": code is not None,
            "has_db": bool((run.get("sandbox_report") or {}).get("db")),
            "model_info": {
                "prompts": [prompts.TRIAGE_VERSION, prompts.INVESTIGATE_VERSION, prompts.HYPOTHESIZE_VERSION],
                "llm_enabled": deps.llm_enabled,
                "code_context": deps.code_enabled,
            },
        }

    async def triage(state: DiagnosisState) -> DiagnosisState:
        finding = await deps.ports.finding()
        out = await _llm(
            LLMRequest(
                "diagnosis.triage",
                prompts.TRIAGE_SYSTEM,
                [
                    {
                        "role": "user",
                        "content": f"Finding:\n{payload(finding)}\nEvidence:\n{payload(_evidence_listing(state))}",
                    }
                ],
                prompts.TRIAGE_OUTPUT,
            ),
            state,
        )
        return {
            "triage": out or {},
            "budget_exhausted": bool(state.get("budget_exhausted")),
            "model_info": state.get("model_info", {}),
        }

    async def investigate(state: DiagnosisState) -> DiagnosisState:
        if not deps.llm_enabled or state.get("budget_exhausted"):
            return {}
        await deps.ports.progress("collecting")
        run = await deps.ports.run_data()
        code = await deps.ports.code() if deps.code_enabled else None
        registry = build_tools(deps.budget, sink, run, code, deps.telemetry)
        finding = await deps.ports.finding()
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Finding:\n{payload(finding)}\nTriage:\n{payload(state.get('triage', {}))}\n"
                    f"Evidence collected so far:\n{payload(_evidence_listing(state))}"
                ),
            }
        ]
        notes, calls = "", 0
        while calls < MAX_TOOL_CALLS:
            resp = await _llm(
                LLMRequest("diagnosis.investigate", prompts.INVESTIGATE_SYSTEM, messages, tools=registry.specs()), state
            )
            if resp is None:
                break
            if not resp.tool_calls:
                notes = resp.text[:8000]
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for call in resp.tool_calls:
                calls += 1
                try:
                    out = await registry.call(call["name"], call.get("input") or {})
                except BudgetExhausted:
                    out = "error: tool budget exhausted"
                    calls = MAX_TOOL_CALLS
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
            messages.append({"role": "user", "content": results})
        evidence = list(state.get("evidence", [])) + [
            asdict(e) for e in created if e.id not in {x["id"] for x in state.get("evidence", [])}
        ]
        return {
            "evidence": evidence,
            "notes": notes,
            "budget_exhausted": bool(state.get("budget_exhausted")),
            "model_info": {**state.get("model_info", {}), "tool_calls": calls},
        }

    async def hypothesize(state: DiagnosisState) -> DiagnosisState:
        await deps.ports.progress("hypothesizing")
        evs = _ev(state)
        pack = pack_mod.Pack(
            finding=await deps.ports.finding(),
            endpoint=state.get("endpoint"),
            items=list(evs.values()),
            facts=state.get("facts", {}),
        )
        candidates = rules.generate(pack)
        out = await _llm(
            LLMRequest(
                "diagnosis.hypothesize",
                prompts.HYPOTHESIZE_SYSTEM,
                [
                    {
                        "role": "user",
                        "content": (
                            f"Finding:\n{payload(pack.finding)}\nEvidence:\n{payload(_evidence_listing(state))}\n"
                            f"Investigation notes:\n{payload(state.get('notes', ''))}"
                        ),
                    }
                ],
                prompts.HYPOTHESES_OUTPUT,
            ),
            state,
        )
        next_steps: list[str] = []
        if out:
            for h in out.get("hypotheses", [])[:MAX_HYPOTHESES]:
                candidates.append({**h, "origin": "model"})
            next_steps = [str(s)[:300] for s in out.get("next_steps", [])[:5]]
        return {
            "candidates": candidates,
            "next_steps": next_steps,
            "budget_exhausted": bool(state.get("budget_exhausted")),
            "model_info": state.get("model_info", {}),
        }

    async def check_citations(state: DiagnosisState) -> DiagnosisState:
        await deps.ports.progress("checking")
        evs = _ev(state)
        allowed = set(evs)
        code = await deps.ports.code() if state.get("has_code") else None
        checked: dict[tuple[str, str], dict[str, Any]] = {}
        for h in state.get("candidates", []):
            cites = []
            for relation, field_name in (
                ("supports", "supporting_evidence"),
                ("contradicts", "contradicting_evidence"),
            ):
                for c in h.get(field_name, []):
                    result, reason = check_claim(str(c.get("claim", "")), evs.get(str(c.get("evidence_id"))), allowed)
                    cites.append(
                        {
                            "evidence_id": str(c.get("evidence_id")),
                            "claim": str(c.get("claim", ""))[:500],
                            "relation": relation,
                            "check_result": result,
                            "reason": reason,
                        }
                    )
            locations = [loc for loc in h.get("code_locations", []) if check_code_location(loc, code)]
            verified = [c for c in cites if c["relation"] == "supports" and c["check_result"] == "verified"]
            if not verified:
                continue  # never shown as a root cause
            entry = {
                **h,
                "code_locations": [{**loc, "commit": code.commit if code else None} for loc in locations],
                "citations": cites,
            }
            key = (h["category"], locations[0]["path"] if locations else "")
            prior = checked.get(key)
            if prior is None:
                checked[key] = entry
            else:  # merge duplicates (rules + model) keeping both sets of verified citations
                known = {(c["evidence_id"], c["relation"]) for c in prior["citations"]}
                prior["citations"] += [c for c in cites if (c["evidence_id"], c["relation"]) not in known]
                if entry["origin"] == "model":
                    prior.update({k: entry[k] for k in ("statement", "mechanism", "fix_direction")})
                    prior["origin"] = "model+rules"
        proposed = sum(len(h.get("supporting_evidence", [])) for h in state.get("candidates", []))
        verified_total = sum(
            1
            for h in checked.values()
            for c in h["citations"]
            if c["relation"] == "supports" and c["check_result"] == "verified"
        )
        return {
            "checked": list(checked.values()),
            "model_info": {
                **state.get("model_info", {}),
                "citations_proposed": proposed,
                "citations_verified": verified_total,
            },
        }

    def route_after_check(state: DiagnosisState) -> str:
        if not state.get("checked"):
            return "score"
        if (
            deps.experiments_enabled
            and state.get("has_db")
            and state.get("endpoint")
            and any(h["category"] in COST_CATEGORIES | CONTENTION_CATEGORIES for h in state["checked"])
        ):
            return "start_experiment"
        return "score"

    async def start_experiment(state: DiagnosisState) -> DiagnosisState:
        await deps.ports.progress("experimenting")
        vus = int((state.get("facts") or {}).get("vus") or 10)
        started = await deps.ports.start_experiment(str(state["endpoint"]), max(5, min(vus, 50)))
        return {"experiment": started}

    async def await_experiment(state: DiagnosisState) -> DiagnosisState:
        exp = state.get("experiment")
        if not exp:
            return {}
        results = interrupt({"waiting_for": "experiment", "runs": exp.get("runs", [])})
        return {"experiment": {**exp, "results": results}}

    async def score_and_rank(state: DiagnosisState) -> DiagnosisState:
        evs = _ev(state)
        exp_results = ((state.get("experiment") or {}).get("results")) or None
        cap = None
        if not state.get("has_code"):
            cap = "medium"
        elif not state.get("has_db") and not any(e.kind == "trace" for e in evs.values()):
            cap = "medium"  # no server-side telemetry (ADR-0011)
        exp_ev: Evidence | None = None
        if exp_results and exp_results.get("low") and exp_results.get("high"):
            low, high = exp_results["low"], exp_results["high"]
            exp_ev = await sink.add_evidence(
                "experiment",
                {
                    "name": "concurrency_contrast",
                    "endpoint": state.get("endpoint"),
                    "runs": (state.get("experiment") or {}).get("runs"),
                },
                f"{state.get('endpoint')}: p95 {float(low.get('p95_ms') or 0):.0f} ms and errors "
                f"{float(low.get('error_rate') or 0):.1%} at 1 VU vs p95 {float(high.get('p95_ms') or 0):.0f} ms and "
                f"errors {float(high.get('error_rate') or 0):.1%} at {exp_results.get('vus')} VUs",
                exp_results,
            )
        ranked = []
        for h in state.get("checked", []):
            verified = [c for c in h["citations"] if c["relation"] == "supports" and c["check_result"] == "verified"]
            contradicting = [
                c for c in h["citations"] if c["relation"] == "contradicts" and c["check_result"] == "verified"
            ]
            kinds = sorted({evs[c["evidence_id"]].kind for c in verified if c["evidence_id"] in evs})
            experiment = None
            if exp_ev is not None and exp_results is not None:
                experiment = interpret_experiment(h["category"], exp_results)
                if experiment["result"] in ("supports", "contradicts"):
                    h["citations"].append(
                        {
                            "evidence_id": exp_ev.id,
                            "relation": experiment["result"],
                            "claim": f"concurrency contrast {experiment['result']} {h['category']}",
                            "check_result": "verified",
                            "reason": "deterministic experiment",
                        }
                    )
            hot = any(
                (
                    evs[c["evidence_id"]].kind == "code_location"
                    and evs[c["evidence_id"]].locator.get("path") in (state.get("handler_path"), None)
                )
                or evs[c["evidence_id"]].locator.get("signal") == "pool_setting"
                for c in verified
                if c["evidence_id"] in evs
            )
            temporal = any(
                evs[c["evidence_id"]].kind in ("metric_window", "http_sample", "db_stat")
                for c in verified
                if c["evidence_id"] in evs
            )
            failed = sum(1 for c in h["citations"] if c["check_result"] == "failed")
            value, band, factors = score(
                Factors(len(verified), kinds, temporal, experiment, len(contradicting), hot, failed, cap)
            )
            ranked.append(
                {
                    **h,
                    "score": value,
                    "confidence": band,
                    "confidence_factors": factors,
                    "status": "supported" if len(verified) >= 2 and not contradicting else "weak",
                }
            )
        ranked.sort(key=lambda h: (-h["score"], h["category"]))
        ranked = ranked[:MAX_HYPOTHESES]
        next_steps = list(state.get("next_steps") or [])
        if not ranked:
            if not state.get("has_code"):
                next_steps.append(DEFAULT_NEXT_STEPS["no_code"])
            if not state.get("has_db"):
                next_steps.append(DEFAULT_NEXT_STEPS["no_db"])
            if not any(e.kind == "trace" for e in evs.values()):
                next_steps.append(DEFAULT_NEXT_STEPS["no_traces"])
        evidence = list(state.get("evidence", [])) + [
            asdict(e) for e in created if e.id not in {x["id"] for x in state.get("evidence", [])}
        ]
        status = "completed" if ranked else ("budget_exhausted" if state.get("budget_exhausted") else "inconclusive")
        return {
            "evidence": evidence,
            "result": {
                "status": status,
                "hypotheses": ranked,
                "next_steps": next_steps,
                "model_info": {**state.get("model_info", {}), "tokens": deps.budget.spent},
            },
        }

    async def persist(state: DiagnosisState) -> DiagnosisState:
        await deps.ports.persist(state["result"])
        return {}

    graph = StateGraph(DiagnosisState)
    for fn in (
        collect_context,
        triage,
        investigate,
        hypothesize,
        check_citations,
        start_experiment,
        await_experiment,
        score_and_rank,
        persist,
    ):
        graph.add_node(fn.__name__, fn)
    graph.add_edge(START, "collect_context")
    graph.add_edge("collect_context", "triage")
    graph.add_edge("triage", "investigate")
    graph.add_edge("investigate", "hypothesize")
    graph.add_edge("hypothesize", "check_citations")
    graph.add_conditional_edges(
        "check_citations", route_after_check, {"start_experiment": "start_experiment", "score": "score_and_rank"}
    )
    graph.add_edge("start_experiment", "await_experiment")
    graph.add_edge("await_experiment", "score_and_rank")
    graph.add_edge("score_and_rank", "persist")
    graph.add_edge("persist", END)
    return graph


async def start(deps: DiagnosisDeps, thread_id: str) -> dict[str, Any]:
    return await _invoke(deps, thread_id, {})


async def resume(deps: DiagnosisDeps, thread_id: str, results: dict[str, Any] | None) -> dict[str, Any]:
    return await _invoke(deps, thread_id, Command(resume=results))


async def _invoke(deps: DiagnosisDeps, thread_id: str, value: Any) -> dict[str, Any]:
    async with checkpointer() as saver:
        app = build(deps).compile(checkpointer=saver)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        out = await app.ainvoke(value, config)
        pending = out.get("__interrupt__") if isinstance(out, dict) else None
        if pending:
            return {"waiting": pending[0].value}
        return dict(json.loads(json.dumps((out or {}).get("result") or {}, default=str)))
