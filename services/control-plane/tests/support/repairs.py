"""Corpus repair fixtures and an "oracle agent" fake model (benchmarks/reference-apps/*/repairs).

The oracle drives the real repair graph through the real tools: it plans the fixture's files,
writes the regression test with ``write_file`` and applies the oracle (or decoy) diff with
``apply_patch``. Everything after that — sandbox checks, verification gates, approval, PR — is
the production code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reliability.llm.fakes import request_payload
from reliability.llm.providers import FakeClient
from reliability.llm.types import LLMRequest, LLMResponse

CORPUS = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps"


@dataclass(frozen=True)
class RepairFixture:
    app: str
    defect: str
    test_path: str
    test: str
    fix: str
    fix_files: list[str]
    decoy: str
    decoy_files: list[str]
    decoy_caught_by: str
    approach: str


def fixture(app: str, defect: str) -> RepairFixture:
    root = CORPUS / app / "repairs"
    spec = json.loads((root / "manifest.json").read_text())[defect]
    return RepairFixture(
        app,
        defect,
        spec["test_path"],
        (root / spec["test"]).read_text(),
        (root / spec["fix"]).read_text(),
        spec["fix_files"],
        (root / spec["decoy"]).read_text(),
        spec["decoy_files"],
        spec["decoy_caught_by"],
        spec["approach"],
    )


def all_fixtures() -> list[RepairFixture]:
    out = []
    for app in ("fastapi-tasks", "fastapi-shop"):
        for defect in json.loads((CORPUS / app / "repairs" / "manifest.json").read_text()):
            out.append(fixture(app, defect))
    return out


def _response(blocks: list[dict[str, Any]]) -> LLMResponse:
    return LLMResponse(model="fake/oracle", content=blocks, stop_reason="end_turn", input_tokens=100, output_tokens=50)


def install_oracle(fake: FakeClient, fx: RepairFixture, variant: str = "fix") -> None:
    files = fx.fix_files if variant == "fix" else fx.decoy_files
    diff = fx.fix if variant == "fix" else fx.decoy

    def plan(_: LLMRequest) -> dict[str, Any]:
        return {
            "files": [*files, fx.test_path],
            "approach": fx.approach,
            "test_idea": f"{fx.test_path} reproduces {fx.defect} deterministically",
            "explanation": fx.approach,
        }

    def code(request: LLMRequest) -> LLMResponse:
        phase = (request_payload(request) or {}).get("phase")
        if any(m.get("role") == "assistant" for m in request.messages):
            return _response([{"type": "text", "text": f"Done: {fx.approach}."}])
        if phase == "test":
            call = {"name": "write_file", "input": {"path": fx.test_path, "content": fx.test}}
        else:
            call = {"name": "apply_patch", "input": {"diff": diff}}
        return _response([{"type": "tool_use", "id": f"call-{phase}", **call}])

    fake.register("repair.plan", plan)
    fake.register("repair.code", code)


# ----------------------------------------------------------- end-to-end ----
REPAIR_DONE = {
    "awaiting_approval",
    "verification_failed",
    "failed",
    "budget_exhausted",
    "rejected",
    "abandoned",
    "approved",
    "pr_opened",
}
RUNNER_TYPES = ["functional", "load", "probe", "sandbox_env", "repair_check"]


async def serve_ready(cfg: Any) -> int:
    """Lease whatever is runnable right now: sandbox runs that are planned and repair checks."""
    import asyncio

    from runner_agent.client import LeaseClient
    from runner_agent.main import serve
    from sqlalchemy import func, select

    from reliability.modules.runs.models import ExecutionJob, Run
    from reliability.platform.db import system_tx

    async with system_tx() as s:
        runs = await s.scalar(
            select(func.count()).where(
                Run.status == "queued",
                Run.environment_kind == "sandbox_build",
                Run.build_ref["status"].astext == "ready",
            )
        )
        jobs = await s.scalar(
            select(func.count()).where(ExecutionJob.status == "queued", ExecutionJob.job_type == "repair_check")
        )
    n = int(runs or 0) + int(jobs or 0)
    if n:
        # A job that can never be leased must fail the test, not hang it.
        await asyncio.wait_for(serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=n), 900)
    return n


async def wait_repair(client: Any, h: dict[str, str], repair_id: str, cfg: Any, limit_s: float = 900) -> dict[str, Any]:
    """Drive outbox + runner until the repair (or its follow-up attempt) settles."""
    import asyncio
    import time

    from reliability.platform.outbox import drain_in_process

    deadline = time.monotonic() + limit_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        await drain_in_process()
        await serve_ready(cfg)
        await drain_in_process()
        body = (await client.get(f"/v1/repairs/{repair_id}", headers=h)).json()
        if body.get("status") in REPAIR_DONE:
            return body
        await asyncio.sleep(0.3)
    raise AssertionError(f"repair {repair_id} stuck in {body.get('status')}: {body.get('status_reason')}")


async def submit_candidate(hypothesis_id: str, requested_by: Any, fx: RepairFixture, variant: str) -> str:
    """Hand a fixed candidate (oracle or decoy) straight to verification, bypassing the agent —
    measures what the independent gates accept."""
    import uuid as _uuid

    from reliability.agents.discovery.snapshot import from_archive
    from reliability.agents.repair.workspace import Workspace
    from reliability.modules.artifacts import service as artifacts
    from reliability.modules.diagnosis.models import Diagnosis, Hypothesis
    from reliability.modules.findings.models import Finding
    from reliability.modules.repairs import service
    from reliability.modules.verification import policy
    from reliability.platform.db import system_tx, tenant_tx

    async with system_tx() as s:
        h = await s.get(Hypothesis, _uuid.UUID(hypothesis_id))
        assert h is not None
        org_id = h.org_id
    async with tenant_tx(org_ids=[org_id]) as s:
        h = await s.get(Hypothesis, _uuid.UUID(hypothesis_id))
        diag = await s.get(Diagnosis, h.diagnosis_id)  # type: ignore[union-attr]
        finding = await s.get(Finding, diag.finding_id)  # type: ignore[union-attr]
        attempt = await service._create(s, finding, h, requested_by, dict(policy.DEFAULT_CONSTRAINTS))  # type: ignore[arg-type]
        attempt.context = {**attempt.context, "auto_retry": False}  # a rejected decoy must not respawn
        base = from_archive(artifacts.read(await artifacts.get(s, _uuid.UUID(attempt.context["base_archive_id"]))))
        ws = Workspace(base)
        ws.write(fx.test_path, fx.test)
        ws.apply_patch(fx.fix if variant == "fix" else fx.decoy)
        changes = ws.changes()
        await service.submit_candidate(
            s,
            attempt,
            {
                "status": "candidate",
                "changes": {p: list(v) for p, v in changes.items() if p != fx.test_path},
                "test_changes": {fx.test_path: list(changes[fx.test_path])},
                "test_paths": [fx.test_path],
                "explanation": f"{variant}: {fx.approach}",
            },
        )
        return str(attempt.id)


async def check_outputs() -> list[dict[str, Any]]:
    """Every repair_check job's outcome (for assertion messages)."""
    from sqlalchemy import select

    from reliability.modules.runs.models import ExecutionJob
    from reliability.platform.db import system_tx

    async with system_tx() as s:
        jobs = (
            await s.execute(
                select(ExecutionJob).where(ExecutionJob.job_type == "repair_check").order_by(ExecutionJob.queued_at)
            )
        ).scalars()
        out = []
        for j in jobs:
            res = j.result or {}
            out.append(
                {
                    "stage": (j.spec.get("notify_payload") or {}).get("stage") or "verification",
                    "status": j.status,
                    "outcome": res.get("outcome"),
                    "error": res.get("abort_reason"),
                    "sandbox_error": ((res.get("result") or {}).get("sandbox") or {}).get("error"),
                    "checks": [
                        (c.get("id"), c.get("exit_code"), (c.get("output") or "")[-600:])
                        for c in (res.get("result") or {}).get("checks") or []
                    ],
                }
            )
        return out
