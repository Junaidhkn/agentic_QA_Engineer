"""Seeded-defect scenarios (evaluation.md §3): run a reference app with one defect switched on
in a sandbox, let detection produce a finding, diagnose it, and score the diagnosis against
the defect's label (category + location). Shared by the Phase 7 E2E test and the benchmark."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from runner_agent.client import LeaseClient
from runner_agent.config import Config
from runner_agent.main import serve

from reliability.platform.outbox import drain_in_process
from tests.conftest import SUPERUSER_URL, auth, make_org

CORPUS = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps"
AUTH_STEPS = [
    {
        "id": "signup",
        "request": {
            "method": "POST",
            "path": "/auth/signup",
            "json": {"email": "{{ email }}", "password": "Pw-12345678", "name": "Bench"},
        },
    },
    {
        "id": "login",
        "request": {
            "method": "POST",
            "path": "/auth/login",
            "json": {"email": "{{ email }}", "password": "Pw-12345678"},
        },
        "extract": {"token": "$.access_token"},
    },
]
BEARER = {"Authorization": "Bearer {{ token }}"}


def _ir(key: str, steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "ir_version": "1.0",
        "key": key,
        "title": key,
        "kind": "api",
        "criticality": "high",
        "data": {"vars": {"email": {"faker": "internet.email"}, "n": {"range": [0, 19999]}}},
        "steps": steps,
        "load": {"weight": 1, "think_time_s": [0, 0.02]},
        **extra,
    }


TASKS_PROJECT = {
    "id": "projects",
    "request": {"method": "GET", "path": "/api/projects"},
    "extract": {"project_id": "$[0].id"},
}
WORKFLOWS: dict[str, dict[str, Any]] = {
    "tasks.lookup": _ir(
        "tasks.lookup",
        [
            TASKS_PROJECT,
            {
                "id": "find",
                "request": {"method": "GET", "path": "/api/tasks", "query": {"assignee": "user{{ n }}@example.com"}},
            },
        ],
    ),
    "tasks.project-list": _ir(
        "tasks.project-list",
        [
            TASKS_PROJECT,
            {"id": "list", "request": {"method": "GET", "path": "/api/projects/{{ project_id }}/tasks"}},
        ],
    ),
    "tasks.create": _ir(
        "tasks.create",
        [
            TASKS_PROJECT,
            {
                "id": "create",
                "request": {
                    "method": "POST",
                    "path": "/api/tasks",
                    "json": {"project_id": "{{ project_id }}", "title": "bench", "assignee_email": "{{ email }}"},
                },
                "expect": {"status": [201]},
            },
        ],
    ),
    "shop.buy-limited": _ir(
        "shop.buy-limited",
        [
            *AUTH_STEPS,
            {
                "id": "order",
                "request": {
                    "method": "POST",
                    "path": "/api/orders",
                    "headers": BEARER,
                    "json": {"product_id": 3, "quantity": 1},
                },
                "expect": {"status": [201, 409]},
            },
        ],
        invariants=[
            {
                "id": "stock-never-negative",
                "type": "concurrency_probe",
                "description": "limited stock is never oversold",
                "parallel": 60,
                "step_ref": "order",
                "check": {
                    "request": {"method": "GET", "path": "/api/products/3"},
                    "jsonpath": "$.stock",
                    "op": ">=",
                    "value": 0,
                },
            }
        ],
    ),
    "shop.pay": _ir(
        "shop.pay",
        [
            *AUTH_STEPS,
            {
                "id": "order",
                "request": {
                    "method": "POST",
                    "path": "/api/orders",
                    "headers": BEARER,
                    "json": {"product_id": 1, "quantity": 1},
                },
                "extract": {"order_id": "$.id"},
            },
            {"id": "pay", "request": {"method": "POST", "path": "/api/orders/{{ order_id }}/pay", "headers": BEARER}},
        ],
    ),
    "shop.search": _ir(
        "shop.search",
        [
            {"id": "search", "request": {"method": "GET", "path": "/api/products", "query": {"q": "coffee"}}},
        ],
    ),
}


@dataclass
class Case:
    app: str
    defect: str
    workflows: list[str]
    kind: str = "load"  # load | probe
    vus: int = 10
    duration_s: int = 15
    p95_ms: float = 70
    error_rate: float = 0.01
    finding_type: str | None = None
    endpoint: str | None = None
    metric: str | None = None  # the breached metric the label describes (e.g. error_rate)

    @property
    def label(self) -> dict[str, Any]:
        labels = json.loads((CORPUS / self.app / "labels.json").read_text())
        return dict(labels["defects"][self.defect])


CASES = [
    Case("fastapi-tasks", "missing_index", ["tasks.lookup"], endpoint="GET /api/tasks"),
    Case("fastapi-tasks", "n_plus_one", ["tasks.project-list"], endpoint="GET /api/projects/{{ project_id }}/tasks"),
    Case("fastapi-tasks", "pool_exhaustion", ["tasks.create"], endpoint="POST /api/tasks"),
    Case("fastapi-shop", "race_stock", ["shop.buy-limited"], kind="probe", finding_type="invariant_violation"),
    Case(
        "fastapi-shop",
        "pay_errors",
        ["shop.pay"],
        duration_s=20,
        endpoint="POST /api/orders/{{ order_id }}/pay",
        metric="error_rate",
    ),
    Case("fastapi-shop", "slow_search", ["shop.search"], p95_ms=45, endpoint="GET /api/products"),
]


@dataclass
class Outcome:
    case: Case
    finding: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    rank_hit: int | None = None
    citations_proposed: int = 0
    citations_verified: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        hyps = (self.diagnosis or {}).get("hypotheses") or []
        return {
            "app": self.case.app,
            "defect": self.case.defect,
            "expected": self.case.label.get("category"),
            "finding": (self.finding or {}).get("title"),
            "status": (self.diagnosis or {}).get("status"),
            "ranked": [(h["category"], h["confidence"]) for h in hyps],
            "rank_hit": self.rank_hit,
            "citations_proposed": self.citations_proposed,
            "citations_verified": self.citations_verified,
            "notes": self.notes,
        }


# Benchmark metadata never ships with the app: labels, the defect README and repair fixtures
# would hand the agent the answers.
NOT_SOURCE = {"labels.json", "README.md"}


def archive(app: str) -> bytes:
    root = CORPUS / app
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            if (
                p.is_file()
                and not {"__pycache__", "node_modules", ".next", "repairs"} & set(rel.parts)
                and str(rel) not in NOT_SOURCE
            ):
                tf.add(p, arcname=f"{app}/{rel}")
    return buf.getvalue()


def runner(base: str, tmp_path: Path, job_types: list[str], k6: str) -> Config:
    from reliability.modules.runners.auth import issue_runner_token

    return Config(
        env="test",
        api_url=base,
        runner_id="bench-runner",
        runner_token=issue_runner_token("bench-runner"),
        driver="process",
        k6_binary=k6,
        workdir=str(tmp_path),
        slots=1,
        job_types=job_types,
        sandbox_postgres_url=SUPERUSER_URL,
        sandbox_inherit_site_packages=True,
    )


def matches(h: dict[str, Any], label: dict[str, Any]) -> bool:
    if h["category"] != label.get("category"):
        return False
    paths = {loc.get("path") for loc in h.get("code_locations", [])}
    text = f"{h.get('statement', '')} {h.get('mechanism', '')}".lower()
    return bool(paths & set(label.get("files", []))) or any(t.lower() in text for t in label.get("targets", []))


async def _idem(client: httpx.AsyncClient, url: str, h: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    return await client.post(url, headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=body)


async def _wait(
    client: httpx.AsyncClient, url: str, h: dict[str, str], done: set[str], limit_s: float, tick: Any = None
) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        await drain_in_process()
        if tick is not None:
            await tick()
        body = (await client.get(url, headers=h)).json()
        if body.get("status") in done:
            return body
        await asyncio.sleep(0.5)
    raise AssertionError(f"{url} did not reach {done}; last {body.get('status')}")


async def run_case(client: httpx.AsyncClient, base: str, tmp_path: Path, case: Case, k6: str) -> Outcome:
    out = Outcome(case)
    org = make_org(f"bench-{case.app[:8]}-{case.defect[:10]}".replace("_", "-")[:30] + uuid.uuid4().hex[:4])
    h = auth(org.email)
    pid = org.project_id
    assert (
        await client.put(f"/v1/projects/{pid}/secrets/SANDBOX_DEFECTS", headers=h, json={"value": case.defect})
    ).status_code == 204
    up = await client.post(
        f"/v1/projects/{pid}/source-archives",
        content=archive(case.app),
        headers={**h, "Content-Type": "application/octet-stream"},
    )
    archive_id = up.json()["archive_artifact_id"]
    for key in case.workflows:
        r = await client.post(f"/v1/projects/{pid}/workflows", headers=h, json={"spec": WORKFLOWS[key]})
        assert r.status_code == 201, r.text
    suite = (await _idem(client, f"/v1/projects/{pid}/test-suites", h, {})).json()
    body: dict[str, Any] = {
        "kind": case.kind,
        "suite_id": suite["id"],
        "environment": {"type": "sandbox_build", "archive_artifact_id": archive_id},
    }
    if case.kind == "load":
        prof = await client.post(
            f"/v1/projects/{pid}/load-profiles",
            headers=h,
            json={
                "name": "bench",
                "type": "load",
                "max_vus": case.vus,
                "stages": [{"duration_s": case.duration_s, "target": case.vus}],
                "think_time_min_s": 0,
                "think_time_max_s": 0.02,
                "thresholds": {"p95_ms": case.p95_ms, "error_rate": case.error_rate, "abort_error_rate": 1.0},
            },
        )
        assert prof.status_code == 201, prof.text
        body["load_profile_id"] = prof.json()["id"]
    run = await _idem(client, f"/v1/projects/{pid}/runs", h, body)
    assert run.status_code == 202, run.text
    cfg = runner(base, tmp_path, [case.kind, "sandbox_env"], k6)
    await drain_in_process()
    assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
    finished = await _wait(client, f"/v1/runs/{run.json()['id']}", h, {"passed", "failed", "invalid", "errored"}, 60)
    if finished["status"] not in ("passed", "failed"):
        out.notes.append(f"run {finished['status']}: {finished.get('status_reason')}")
        return out
    await drain_in_process()
    findings = (await client.get(f"/v1/projects/{pid}/findings?status=active", headers=h)).json()
    wanted = [
        f
        for f in findings
        if (case.finding_type is None or f["type"] == case.finding_type)
        and (case.endpoint is None or f["headline"].get("scope") == f"endpoint:{case.endpoint}")
        and (case.metric is None or f["headline"].get("metric") == case.metric)
    ]
    if not wanted:
        summaries = (await client.get(f"/v1/runs/{run.json()['id']}/summaries", headers=h)).json()
        out.notes.append(
            f"no matching finding; got {[f['title'] for f in findings]}; status {finished['status']}; "
            f"p95 {[(r['scope'], r.get('p95_ms'), r.get('error_rate')) for r in summaries]}"
        )
        return out
    out.finding = sorted(wanted, key=lambda f: f["severity"])[0]
    diag = await _idem(client, f"/v1/findings/{out.finding['id']}/diagnoses", h, {})
    assert diag.status_code == 202, diag.text
    exp_cfg = runner(base, tmp_path, ["load", "sandbox_env"], k6)

    async def run_experiments() -> None:
        await asyncio.wait_for(_serve_available(exp_cfg), 120)

    result = await _wait(
        client,
        f"/v1/diagnoses/{diag.json()['id']}",
        h,
        {"completed", "inconclusive", "failed", "budget_exhausted"},
        300,
        run_experiments,
    )
    out.diagnosis = result
    info = result.get("model_info") or {}
    out.citations_proposed = int(info.get("citations_proposed") or 0)
    out.citations_verified = int(info.get("citations_verified") or 0)
    for rank, hyp in enumerate(result.get("hypotheses") or [], 1):
        if matches(hyp, case.label):
            out.rank_hit = rank
            break
    return out


async def _serve_available(cfg: Config) -> None:
    """Run whatever sandbox load runs are leasable right now (diagnosis experiments)."""
    from sqlalchemy import select

    from reliability.modules.runs.models import Run
    from reliability.platform.db import system_tx

    async with system_tx() as s:
        ready = (
            await s.execute(
                select(Run.id).where(
                    Run.status == "queued",
                    Run.environment_kind == "sandbox_build",
                    Run.build_ref["status"].astext == "ready",
                )
            )
        ).all()
    if ready:
        await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=len(ready))
