"""Test-generation graph (FR-10, FR-12): dry-run classification, interrupt/resume around the
runner job, model repair of failing IR (≤ 3 attempts) and its guardrails."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from reliability.agents.common import checkpoint
from reliability.agents.common.budget import GraphBudget
from reliability.agents.testgen import graph
from reliability.llm.gateway import Gateway, NullSink
from reliability.llm.providers import FakeClient
from reliability.llm.types import LLMRequest
from reliability.platform.settings import Settings


@pytest.fixture(autouse=True)
def _memory_checkpointer() -> Any:
    checkpoint.use_memory_checkpointer()
    yield
    checkpoint.use_memory_checkpointer(False)


def _ir(key: str, status: int = 201) -> dict[str, Any]:
    return {
        "ir_version": "1.0",
        "key": key,
        "title": key,
        "kind": "api",
        "criticality": "normal",
        "steps": [
            {
                "id": "create",
                "request": {"method": "POST", "path": "/api/items", "json": {"name": "x"}},
                "expect": {"status": [status]},
            }
        ],
    }


class Ports:
    def __init__(self, target: bool = True) -> None:
        self.target = target
        self.compiled: list[dict[str, dict[str, Any]]] = []
        self.jobs: list[list[str]] = []
        self.finished: tuple[dict[str, Any], str, dict[str, Any]] | None = None

    async def compile(self, irs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        self.compiled.append(copy.deepcopy(irs))
        return [{"id": f"case-{k}", "workflow_key": k, "type": "k6_scenario", "validatable": True} for k in irs] + [
            {"id": f"probe-{k}", "workflow_key": k, "type": "k6_probe", "validatable": False} for k in irs
        ]

    async def start_validation(self, case_ids: list[str], repeats: int) -> str | None:
        assert repeats == 3
        self.jobs.append(case_ids)
        return f"job-{len(self.jobs)}" if self.target else None

    async def finish(self, cases: dict[str, dict[str, Any]], status: str, info: dict[str, Any]) -> None:
        self.finished = (cases, status, info)


def _deps(ports: Ports, fake: FakeClient, llm: bool = True) -> graph.TestgenDeps:
    return graph.TestgenDeps(
        ports,
        Gateway(fake, Settings(llm_provider="fake")),
        NullSink(),
        GraphBudget.for_graph("testgen"),
        uuid.uuid4(),
        uuid.uuid4(),
        llm,
    )


def _results(ids: list[str], codes: list[int], log: str = "status 422 != 201") -> dict[str, Any]:
    return {cid: [{"exit_code": c, "log_tail": log if c else None} for c in codes] for cid in ids}


def test_classify() -> None:
    assert graph.classify_outcomes([{"exit_code": 0}] * 3) == "valid"
    assert graph.classify_outcomes([{"exit_code": 0}, {"exit_code": 1}, {"exit_code": 0}]) == "flaky"
    assert graph.classify_outcomes([{"exit_code": 1}] * 3) == "failed"
    assert graph.classify_outcomes([]) == "failed"


async def test_valid_and_flaky_without_repair() -> None:
    ports, fake = Ports(), FakeClient()
    deps, thread = _deps(ports, fake), f"testgen:{uuid.uuid4()}"
    waiting = await graph.start(deps, thread, {"a.one": _ir("a.one"), "b.two": _ir("b.two")})
    assert waiting["waiting"]["job_id"] == "job-1" and ports.jobs[0] == ["case-a.one", "case-b.two"]
    out = await graph.resume(
        deps, thread, {**_results(["case-a.one"], [0, 0, 0]), **_results(["case-b.two"], [0, 1, 0])}
    )
    assert out["status"] == "ready" and not fake.calls
    cases, status, _ = ports.finished  # type: ignore[misc]
    assert status == "ready"
    assert {k: v["status"] for k, v in cases.items()} == {
        "case-a.one": "valid",
        "case-b.two": "flaky",
        "probe-a.one": "pending",
        "probe-b.two": "pending",
    }
    assert cases["case-b.two"]["detail"]["passed"] == 2


async def test_failing_case_is_repaired_and_revalidated() -> None:
    ports, fake = Ports(), FakeClient()
    seen: list[LLMRequest] = []

    def repair(req: LLMRequest) -> dict[str, Any]:
        seen.append(req)
        return {"ir": _ir("a.one", status=200), "rationale": "API returns 200, not 201"}

    fake.register("testgen.repair", repair)
    deps, thread = _deps(ports, fake), f"testgen:{uuid.uuid4()}"
    await graph.start(deps, thread, {"a.one": _ir("a.one")})
    second = await graph.resume(deps, thread, _results(["case-a.one"], [1, 1, 1], "expected 201 got 200"))
    assert second["waiting"]["job_id"] == "job-2"  # recompiled and dry-run again
    assert ports.compiled[-1]["a.one"]["steps"][0]["expect"]["status"] == [200]
    assert '<data kind="dry_run_output"' in seen[0].messages[0]["content"]
    out = await graph.resume(deps, thread, _results(["case-a.one"], [0, 0, 0]))
    assert out["status"] == "ready"
    cases, _, info = ports.finished  # type: ignore[misc]
    assert cases["case-a.one"]["status"] == "valid"
    assert info["repaired"] == {"a.one": "API returns 200, not 201"}
    assert info["repaired_irs"]["a.one"]["steps"][0]["expect"]["status"] == [200]


async def test_invalid_after_three_repairs() -> None:
    ports, fake = Ports(), FakeClient()
    n = {"i": 0}

    def repair(req: LLMRequest) -> dict[str, Any]:
        n["i"] += 1
        return {"ir": _ir("a.one", status=201 + n["i"]), "rationale": f"try {n['i']}"}

    fake.register("testgen.repair", repair)
    deps, thread = _deps(ports, fake), f"testgen:{uuid.uuid4()}"
    await graph.start(deps, thread, {"a.one": _ir("a.one")})
    out: dict[str, Any] = {}
    for _ in range(4):
        out = await graph.resume(deps, thread, _results(["case-a.one"], [1, 1, 1]))
        if "status" in out:
            break
    assert n["i"] == graph.MAX_REPAIRS and out["status"] == "partially_ready"  # probe case stays pending
    cases, _, _ = ports.finished  # type: ignore[misc]
    assert cases["case-a.one"]["status"] == "invalid" and "expected" not in cases["case-a.one"]["detail"]


async def test_repair_guardrails() -> None:
    ports, fake = Ports(), FakeClient()
    bad = _ir("a.one")
    bad["steps"].append({"id": "wipe", "request": {"method": "DELETE", "path": "/api/admin/all"}})
    fake.register("testgen.repair", lambda r: {"ir": bad, "rationale": "add cleanup"})
    deps, thread = _deps(ports, fake), f"testgen:{uuid.uuid4()}"
    await graph.start(deps, thread, {"a.one": _ir("a.one")})
    out = await graph.resume(deps, thread, _results(["case-a.one"], [1, 1, 1]))
    assert out["status"] == "partially_ready"
    detail = ports.finished[0]["case-a.one"]["detail"]  # type: ignore[index]
    assert "unknown endpoints" in detail["repair_rejected"][0] and len(ports.jobs) == 1


async def test_no_target_leaves_cases_pending() -> None:
    ports, fake = Ports(target=False), FakeClient()
    out = await graph.start(_deps(ports, fake), f"testgen:{uuid.uuid4()}", {"a.one": _ir("a.one")})
    assert out["status"] == "ready"
    cases, _, _ = ports.finished  # type: ignore[misc]
    assert cases["case-a.one"]["status"] == "pending"
    assert cases["case-a.one"]["detail"]["reason"].startswith("no verified target")
