"""Diagnosis (FR-21…23, ADR-0015): citation checks, rubric, code analysis, rule-based
hypotheses and the graph (model tool loop, discarded uncited hypotheses, inconclusive results,
experiment interrupt/resume)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from reliability.agents.common import checkpoint
from reliability.agents.common.budget import GraphBudget
from reliability.agents.diagnosis import evidence as pack_mod
from reliability.agents.diagnosis import graph, rules
from reliability.agents.diagnosis.citations import Evidence, check_claim, check_code_location
from reliability.agents.diagnosis.code import CodeIndex
from reliability.agents.diagnosis.rubric import Factors, score
from reliability.agents.discovery.snapshot import from_files
from reliability.llm.gateway import Gateway, NullSink
from reliability.llm.providers import FakeClient
from reliability.llm.types import LLMRequest, LLMResponse
from reliability.platform.settings import Settings

CORPUS = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps"


def _index(app: str) -> CodeIndex:
    root = CORPUS / app
    files = {
        str(p.relative_to(root)): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and not {"__pycache__", "node_modules"} & set(p.parts)
    }
    return CodeIndex.build(from_files(files), "c0ffee")


@pytest.fixture(autouse=True)
def _memory_checkpointer() -> Any:
    checkpoint.use_memory_checkpointer()
    yield
    checkpoint.use_memory_checkpointer(False)


# ------------------------------------------------------------ citations ----
EV = Evidence(
    "e1",
    "db_stat",
    "active connections = 10/10 from t+212s; pool wait p95 1.6 s",
    {"stat": "connections", "table": "orders"},
    {"active": 10, "max": 10, "wait_p95_ms": 1600, "error_rate": 0.042},
)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("10/10 connections were busy", "verified"),
        ("pool wait p95 was 1.6 s", "verified"),
        ("pool wait p95 was 1600 ms", "verified"),
        ("4.2% of requests failed", "verified"),
        ("wait p95 1.62 s", "verified"),  # within 5% tolerance
        ("12/10 connections", "failed"),
        ("pool wait p95 2.5 s", "failed"),
        ("the orders table connections were exhausted", "verified"),  # names what the evidence is about
        ("something unrelated entirely", "unverifiable"),
    ],
)
def test_claim_checks(claim: str, expected: str) -> None:
    assert check_claim(claim, EV, {"e1"})[0] == expected


def test_missing_or_foreign_evidence_fails() -> None:
    assert check_claim("10/10", None, {"e1"}) == ("failed", "evidence does not exist")
    assert check_claim("10/10", EV, {"other"})[0] == "failed"


def test_code_location_check() -> None:
    code = _index("fastapi-tasks")
    assert check_code_location({"path": "app/routes.py", "lines": [40, 41]}, code)
    assert not check_code_location({"path": "app/routes.py", "lines": [4000]}, code)
    assert not check_code_location({"path": "app/nope.py", "lines": [1]}, code)
    assert not check_code_location({"path": "app/routes.py", "lines": [1]}, None)


# --------------------------------------------------------------- rubric ----
def test_rubric_is_monotonic_and_capped() -> None:
    base = Factors(1, ["metric_window"])
    richer = Factors(3, ["metric_window", "db_stat", "code_location"], True, hot_path_code=True)
    s1, s2 = score(base)[0], score(richer)[0]
    assert s2 > s1
    supported = score(
        Factors(3, ["metric_window", "db_stat", "code_location"], True, {"result": "supports"}, hot_path_code=True)
    )
    assert supported[0] > s2 and supported[1] == "high"
    contradicted = score(
        Factors(3, ["metric_window", "db_stat", "code_location"], True, {"result": "contradicts"}, hot_path_code=True)
    )
    assert contradicted[0] < s2
    assert (
        score(
            Factors(
                3,
                ["metric_window", "db_stat", "code_location"],
                True,
                {"result": "supports"},
                hot_path_code=True,
                cap="medium",
            )
        )[1]
        == "medium"
    )
    assert score(Factors(0, []))[1] == "low"
    assert score(base)[2]["rubric_version"] == "rubric@1"


# ------------------------------------------------------------ code index ----
def test_code_index_on_reference_apps() -> None:
    tasks = _index("fastapi-tasks")
    h = tasks.handler("GET /api/projects/{{ project_id }}/tasks")
    assert h is not None and h.path == "app/routes.py"
    assert tasks.db_calls_in_loops(h)  # comment count per task
    find = tasks.handler("GET /api/tasks")
    assert find is not None and not tasks.db_calls_in_loops(find)  # query in the iterable runs once
    assert ("tasks", "assignee_email", find.start + 2) in tasks.filter_columns(find)
    assert {p["setting"] for p in tasks.pool_settings()} >= {"pool_size"}
    shop = _index("fastapi-shop")
    order = shop.handler("POST /api/orders")
    assert order is not None and shop.unlocked_read_modify_write(order)
    pay = shop.handler("POST /api/orders/{{ orders_id }}/pay")
    assert pay is not None and shop.generic_raises(pay)


# ---------------------------------------------------------------- rules ----
class MemorySink:
    def __init__(self) -> None:
        self.items: dict[str, Evidence] = {}

    async def add_evidence(
        self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
    ) -> Evidence:
        ev = Evidence(str(uuid.uuid4()), kind, summary, locator, snapshot)
        self.items[ev.id] = ev
        return ev


TASKS_RUN = {
    "id": "run-1",
    "profile": {"max_vus": 20},
    "overall": {"requests": 1000},
    "endpoints": {
        "endpoint:GET /api/tasks": {"p95_ms": 85.0, "error_rate": 0.0, "requests": 300},
        "endpoint:GET /api/projects": {"p95_ms": 4.0, "error_rate": 0.0, "requests": 300},
    },
    "samples": {"errors": [], "slowest": []},
    "sandbox_report": {
        "db": {
            "tables": [
                {"relname": "tasks", "seq_scan": 900, "seq_tup_read": 54000000, "idx_scan": 50, "n_live_tup": 60000}
            ],
            "indexes": [
                {
                    "relname": "tasks",
                    "indexrelname": "tasks_pkey",
                    "idx_scan": 5,
                    "indexdef": "CREATE UNIQUE INDEX tasks_pkey ON public.tasks USING btree (id)",
                }
            ],
            "connections": {"total": 3, "max": 100},
        },
        "db_samples": [{"active": 1, "idle_in_transaction": 0}] * 5,
    },
}


async def test_rules_find_missing_index_with_verifiable_citations() -> None:
    sink = MemorySink()
    finding = {"type": "slo_breach", "headline": {"scope": "endpoint:GET /api/tasks", "at_vus": 20}}
    pack = await pack_mod.collect(sink, finding, [], TASKS_RUN, _index("fastapi-tasks"))
    hyps = rules.generate(pack)
    assert [h["category"] for h in hyps] == ["missing_index"]
    allowed = set(sink.items)
    results = [
        check_claim(c["claim"], sink.items[c["evidence_id"]], allowed)[0] for c in hyps[0]["supporting_evidence"]
    ]
    assert results.count("verified") == len(results) >= 3
    # With the index present at runtime there is no missing-index hypothesis.
    healthy = json.loads(json.dumps(TASKS_RUN))
    healthy["sandbox_report"]["db"]["indexes"].append(
        {
            "relname": "tasks",
            "indexrelname": "tasks_assignee_idx",
            "idx_scan": 900,
            "indexdef": "CREATE INDEX tasks_assignee_idx ON public.tasks USING btree (assignee_email)",
        }
    )
    pack = await pack_mod.collect(MemorySink(), finding, [], healthy, _index("fastapi-tasks"))
    assert not [h for h in rules.generate(pack) if h["category"] == "missing_index"]


def _verified(sink: MemorySink, h: dict[str, Any]) -> list[str]:
    return [check_claim(c["claim"], sink.items[c["evidence_id"]], set(sink.items))[0] for c in h["supporting_evidence"]]


async def test_rules_read_string_statuses_from_k6_samples() -> None:
    sink = MemorySink()
    endpoint = "POST /api/orders/{{ order_id }}/pay"
    run = {
        "id": "run-2",
        "overall": {"requests": 500},
        "endpoints": {f"endpoint:{endpoint}": {"p95_ms": 20.0, "error_rate": 0.059, "requests": 200}},
        # k6 tags carry the status as a string
        "samples": {"errors": [{"endpoint": endpoint, "status": "500", "trace_id": "t1", "duration_ms": 3.1}]},
    }
    finding = {"type": "error_spike", "headline": {"scope": f"endpoint:{endpoint}"}}
    pack = await pack_mod.collect(sink, finding, [], run, _index("fastapi-shop"))
    hyps = rules.generate(pack)
    assert [h["category"] for h in hyps] == ["external_dependency"]
    assert hyps[0]["code_locations"][0]["path"] == "app/orders.py"
    assert set(_verified(sink, hyps[0])) == {"verified"}


@pytest.mark.parametrize(("peak", "expected"), [(2, True), (3, True), (9, False)])
async def test_pool_exhaustion_from_saturated_connections(peak: int, expected: bool) -> None:
    sink = MemorySink()
    run = json.loads(json.dumps(TASKS_RUN))
    run["endpoints"] = {"endpoint:POST /api/tasks": {"p95_ms": 987.0, "error_rate": 0.0, "requests": 200}}
    run["sandbox_report"]["db"]["tables"] = []
    run["sandbox_report"]["db_samples"] = [{"active": 0, "idle_in_transaction": peak}] * 6
    finding = {"type": "slo_breach", "headline": {"scope": "endpoint:POST /api/tasks"}}
    pack = await pack_mod.collect(sink, finding, [], run, _index("fastapi-tasks"))
    pool = [h for h in rules.generate(pack) if h["category"] == "pool_exhaustion"]
    assert bool(pool) is expected  # a peak above the configured size means it is not the limit
    if pool:
        assert set(_verified(sink, pool[0])) == {"verified"}


def test_probe_endpoint_is_the_parallel_step() -> None:
    from reliability.modules.diagnosis.jobs import _probe_endpoint

    spec = {
        "steps": [
            {"id": "login", "request": {"method": "POST", "path": "/auth/login"}},
            {"id": "order", "request": {"method": "POST", "path": "/api/orders"}},
        ],
        "invariants": [{"id": "stock-never-negative", "step_ref": "order"}],
    }
    assert _probe_endpoint(spec, "stock-never-negative") == "POST /api/orders"
    assert _probe_endpoint(spec, "unknown") is None


# ---------------------------------------------------------------- graph ----
class Ports:
    def __init__(
        self, finding: dict[str, Any], run: dict[str, Any], code: CodeIndex | None, experiment: bool = False
    ) -> None:
        self._finding, self._run, self._code, self.experiment = finding, run, code, experiment
        self.sink = MemorySink()
        self.persisted: dict[str, Any] | None = None
        self.statuses: list[str] = []
        self.experiments: list[tuple[str, int]] = []

    async def finding(self) -> dict[str, Any]:
        return self._finding

    async def existing_evidence(self) -> list[Evidence]:
        return []

    async def run_data(self) -> dict[str, Any]:
        return self._run

    async def code(self) -> CodeIndex | None:
        return self._code

    async def add_evidence(
        self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
    ) -> Evidence:
        return await self.sink.add_evidence(kind, locator, summary, snapshot)

    async def progress(self, status: str) -> None:
        self.statuses.append(status)

    async def start_experiment(self, endpoint: str, vus: int) -> dict[str, Any] | None:
        self.experiments.append((endpoint, vus))
        return {"runs": ["r-low", "r-high"], "vus": vus} if self.experiment else None

    async def persist(self, result: dict[str, Any]) -> None:
        self.persisted = result


def _deps(ports: Ports, fake: FakeClient, llm: bool = True, experiments: bool = True) -> graph.DiagnosisDeps:
    return graph.DiagnosisDeps(
        ports,
        Gateway(fake, Settings(llm_provider="fake")),
        NullSink(),
        GraphBudget.for_graph("diagnosis"),
        uuid.uuid4(),
        uuid.uuid4(),
        llm_enabled=llm,
        experiments_enabled=experiments,
    )


FINDING = {
    "id": "f1",
    "type": "slo_breach",
    "title": "GET /api/tasks p95 85 ms exceeds 40 ms",
    "headline": {"scope": "endpoint:GET /api/tasks", "at_vus": 20},
}


async def test_graph_rules_only_completes_with_ranked_cited_hypotheses() -> None:
    ports = Ports(FINDING, TASKS_RUN, _index("fastapi-tasks"))
    out = await graph.start(_deps(ports, FakeClient(), llm=False, experiments=False), f"d:{uuid.uuid4()}")
    assert out["status"] == "completed" and ports.persisted is not None
    top = out["hypotheses"][0]
    assert top["category"] == "missing_index" and top["confidence"] in ("medium", "high")
    assert top["code_locations"][0]["path"] == "app/routes.py" and top["code_locations"][0]["commit"] == "c0ffee"
    assert all(c["check_result"] == "verified" for c in top["citations"] if c["relation"] == "supports")
    assert ports.statuses[:2] == ["collecting", "hypothesizing"]


def _last_evidence_id(req: LLMRequest) -> str | None:
    for m in reversed(req.messages):
        content = m["content"]
        text = json.dumps(content)
        found = re.findall(r"evidence_id: ([0-9a-f-]{36})", text)
        if found:
            return found[-1]
    return None


async def test_graph_with_model_tool_loop_and_citation_filtering() -> None:
    fake = FakeClient()
    calls: list[LLMRequest] = []

    def investigate(req: LLMRequest) -> LLMResponse:
        calls.append(req)
        if len(calls) == 1:
            return LLMResponse(
                "fake",
                [
                    {"type": "thinking", "thinking": "look at the handler", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "read_code",
                        "input": {"path": "app/routes.py", "start_line": 46, "end_line": 48},
                    },
                ],
                "tool_use",
                10,
                10,
            )
        return LLMResponse("fake", [{"type": "text", "text": "The handler filters by assignee."}], "end_turn", 10, 10)

    def hypothesize(req: LLMRequest) -> dict[str, Any]:
        eid = _last_evidence_id(req) or "missing"
        listing = req.messages[0]["content"]
        code_ev = re.findall(r'"id":"([0-9a-f-]{36})","kind":"code_location","summary":"app/routes.py:46-48"', listing)
        assert code_ev, "the evidence listing includes the tool result"
        return {
            "hypotheses": [
                {
                    "category": "unbounded_query",
                    "statement": "invented",
                    "mechanism": "m",
                    "fix_direction": "f",
                    "code_locations": [{"path": "app/routes.py", "lines": [48, 48]}],
                    "supporting_evidence": [
                        {"evidence_id": "00000000-0000-0000-0000-000000000000", "claim": "999 rows"}
                    ],
                    "contradicting_evidence": [],
                },
                {
                    "category": "missing_index",
                    "statement": "Model says: assignee lookups scan tasks.",
                    "mechanism": "Seq scans on tasks for each lookup.",
                    "fix_direction": "index tasks(assignee_email)",
                    "code_locations": [
                        {"path": "app/routes.py", "lines": [46, 48]},
                        {"path": "app/ghost.py", "lines": [1]},
                    ],
                    "supporting_evidence": [{"evidence_id": (code_ev or [eid])[0], "claim": "app/routes.py:46-48"}],
                    "contradicting_evidence": [],
                },
            ],
            "next_steps": [],
        }

    fake.register("diagnosis.investigate", investigate)
    fake.register("diagnosis.hypothesize", hypothesize)
    fake.register("diagnosis.triage", lambda r: {"categories": ["missing_index"], "focus_evidence": []})
    ports = Ports(FINDING, TASKS_RUN, _index("fastapi-tasks"))
    out = await graph.start(_deps(ports, fake, experiments=False), f"d:{uuid.uuid4()}")
    assert out["status"] == "completed"
    # The thinking block was replayed verbatim with the tool result.
    second = calls[1].messages
    assert second[1]["content"][0]["type"] == "thinking" and second[2]["content"][0]["type"] == "tool_result"
    cats = [h["category"] for h in out["hypotheses"]]
    assert "unbounded_query" not in cats  # only an invented citation → discarded
    merged = next(h for h in out["hypotheses"] if h["category"] == "missing_index")
    assert merged["origin"] == "model+rules" and merged["statement"].startswith("Model says")
    assert {loc["path"] for loc in merged["code_locations"]} == {"app/routes.py"}  # ghost file dropped
    assert out["model_info"]["tool_calls"] == 1


async def test_graph_inconclusive_without_evidence() -> None:
    run = {"id": "r", "overall": {"requests": 10}, "endpoints": {}, "samples": {}, "sandbox_report": None}
    ports = Ports({"id": "f", "type": "slo_breach", "headline": {"scope": "endpoint:GET /x"}}, run, None)
    out = await graph.start(_deps(ports, FakeClient(), llm=False), f"d:{uuid.uuid4()}")
    assert out["status"] == "inconclusive" and out["hypotheses"] == []
    assert any("Connect the repository" in s for s in out["next_steps"])


async def test_graph_experiment_interrupt_and_resume() -> None:
    ports = Ports(FINDING, TASKS_RUN, _index("fastapi-tasks"), experiment=True)
    deps, thread = _deps(ports, FakeClient(), llm=False), f"d:{uuid.uuid4()}"
    waiting = await graph.start(deps, thread)
    assert waiting["waiting"]["runs"] == ["r-low", "r-high"] and ports.experiments == [("GET /api/tasks", 20)]
    assert ports.persisted is None
    out = await graph.resume(
        deps,
        thread,
        {"vus": 20, "low": {"p95_ms": 70.0, "error_rate": 0.0}, "high": {"p95_ms": 90.0, "error_rate": 0.0}},
    )
    top = out["hypotheses"][0]
    assert top["confidence_factors"]["experiment"]["result"] == "supports"  # latency present at 1 VU
    assert any(c["relation"] == "supports" and "concurrency contrast" in c["claim"] for c in top["citations"])
    exp = [e for e in ports.sink.items.values() if e.kind == "experiment"]
    assert exp and "at 1 VU" in exp[0].summary


def test_experiment_interpretation() -> None:
    contention = graph.interpret_experiment(
        "pool_exhaustion", {"low": {"p95_ms": 12, "error_rate": 0}, "high": {"p95_ms": 2100, "error_rate": 0.2}}
    )
    assert contention["result"] == "supports"
    assert (
        graph.interpret_experiment("pool_exhaustion", {"low": {"p95_ms": 80}, "high": {"p95_ms": 90}})["result"]
        == "contradicts"
    )
    assert (
        graph.interpret_experiment("missing_index", {"low": {"p95_ms": 5}, "high": {"p95_ms": 900}})["result"]
        == "contradicts"
    )
