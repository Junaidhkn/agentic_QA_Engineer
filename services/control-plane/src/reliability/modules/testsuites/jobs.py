"""Test-generation graph wiring (FR-10…13): graph ports, the ``test_validation`` runner job and
the resume path from the runner's completion back into the waiting graph thread."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.agents.common.budget import GraphBudget
from reliability.agents.testgen import graph
from reliability.llm import get_gateway
from reliability.modules.artifacts import service as artifacts
from reliability.modules.discovery.models import AgentThread
from reliability.modules.quotas.llm_sink import LedgerUsageSink
from reliability.modules.runners import service as runners
from reliability.modules.runners.spec import LIMITS
from reliability.modules.runs.models import ExecutionJob
from reliability.modules.secrets import service as secrets
from reliability.modules.targets import service as targets
from reliability.modules.targets.models import Target
from reliability.modules.tenancy.models import Organization
from reliability.modules.testsuites import service as suites
from reliability.modules.testsuites.models import TestCase, TestSuite
from reliability.modules.workflows.ir import secret_names
from reliability.modules.workflows.models import Workflow
from reliability.platform import outbox
from reliability.platform.actor import Actor
from reliability.platform.crypto import seal_for_x25519
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.flags import is_enabled, kill_switch_active
from reliability.platform.jobs import job

log = logging.getLogger(__name__)
LLM_DISABLED_FLAG = "agents.testgen.llm_disabled"
SECONDS_PER_DRY_RUN = 90


class SuitePorts:
    def __init__(self, suite: TestSuite) -> None:
        self.suite_id, self.org_id = suite.id, suite.org_id
        self.target_id = uuid.UUID(suite.generator_info["validation_target_id"])
        self.workflow_ids = [uuid.UUID(w) for w in suite.generator_info.get("workflow_ids", [])]

    async def _workflows(self, s: AsyncSession) -> list[Workflow]:
        rows = await s.execute(select(Workflow).where(Workflow.id.in_(self.workflow_ids)))
        return list(rows.scalars())

    async def compile(self, irs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        async with tenant_tx(org_ids=[self.org_id]) as s:
            suite = await s.get(TestSuite, self.suite_id, with_for_update=True)
            assert suite is not None
            workflows = [w for w in await self._workflows(s) if w.key in irs]
            cases = await suites.write_bundle(s, suite, workflows, irs)
            suite.status = "validating"
            by_id = {w.id: w.key for w in workflows}
            return [
                {
                    "id": str(c.id),
                    "workflow_key": by_id[c.workflow_id],
                    "type": c.type,
                    "validatable": c.type in ("k6_scenario", "playwright"),
                }
                for c in cases
            ]

    async def start_validation(self, case_ids: list[str], repeats: int) -> str | None:
        async with tenant_tx(org_ids=[self.org_id]) as s:
            target = await s.get(Target, self.target_id)
            if (
                target is None
                or not target.is_runnable
                or targets.policy_violation(target, "smoke", 1, 600)
                or await kill_switch_active(s, self.org_id)
            ):
                return None
            jb = await runners.enqueue_job(
                s,
                org_id=self.org_id,
                job_type="test_validation",
                subject_id=self.suite_id,
                spec={
                    "suite_id": str(self.suite_id),
                    "target_id": str(self.target_id),
                    "case_ids": case_ids,
                    "repeats": repeats,
                },
            )
            thread = await s.get(AgentThread, f"testgen:{self.suite_id}")
            if thread is not None:
                thread.pending_interrupt = {"waiting_for": "test_validation", "job_id": str(jb.id)}
            return str(jb.id)

    async def finish(self, cases: dict[str, dict[str, Any]], status: str, info: dict[str, Any]) -> None:
        async with tenant_tx(org_ids=[self.org_id]) as s:
            suite = await s.get(TestSuite, self.suite_id, with_for_update=True)
            assert suite is not None
            for cid, c in cases.items():
                tc = await s.get(TestCase, uuid.UUID(cid))
                if tc is None:
                    continue
                tc.validation_status = c["status"] if c["status"] in ("valid", "invalid", "flaky") else "pending"
                detail = dict(tc.validation_detail or {})
                detail.update(c.get("detail") or {})
                if c["workflow_key"] in info.get("repaired", {}):
                    detail["repaired"] = True
                tc.validation_detail = detail
            suite.status = status
            suite.generator_info = {**suite.generator_info, **info, "validated_at": datetime.now(UTC).isoformat()}
            thread = await s.get(AgentThread, f"testgen:{self.suite_id}")
            if thread is not None:
                thread.pending_interrupt = None


async def _deps(suite: TestSuite, llm_enabled: bool) -> graph.TestgenDeps:
    scale = max(1, len(suite.generator_info.get("workflow_ids", [])))
    return graph.TestgenDeps(
        SuitePorts(suite),
        get_gateway(),
        LedgerUsageSink(),
        GraphBudget.for_graph("testgen", scale=scale),
        suite.org_id,
        suite.id,
        llm_enabled,
    )


async def _load(payload: dict[str, Any]) -> tuple[TestSuite, bool] | None:
    org_id, suite_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["suite_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        suite = await s.get(TestSuite, suite_id)
        if suite is None or suite.status not in ("generating", "validating"):
            return None
        org = await s.get(Organization, org_id)
        llm = bool(org and org.llm_code_context_enabled) and not await is_enabled(s, LLM_DISABLED_FLAG, org_id)
        return suite, llm


async def _fail(suite: TestSuite, message: str) -> None:
    async with tenant_tx(org_ids=[suite.org_id]) as s:
        row = await s.get(TestSuite, suite.id, with_for_update=True)
        if row is not None:
            row.status = "failed"
            row.generator_info = {**row.generator_info, "error": message[:500]}


@job("testsuites.generate", queue="agents", max_retries=2)
async def generate_suite(payload: dict[str, Any]) -> None:
    loaded = await _load(payload)
    if loaded is None:
        return
    suite, llm = loaded
    async with tenant_tx(org_ids=[suite.org_id]) as s:
        workflows = [
            w
            for w in (
                await s.execute(
                    select(Workflow).where(
                        Workflow.id.in_([uuid.UUID(w) for w in suite.generator_info.get("workflow_ids", [])])
                    )
                )
            ).scalars()
        ]
        irs = {w.key: w.spec for w in workflows}
    try:
        await graph.start(await _deps(suite, llm), suite.agent_thread_id or f"testgen:{suite.id}", irs)
    except AppError as exc:
        await _fail(suite, exc.message)


@job("testsuites.validated", queue="agents", max_retries=2)
async def resume_suite(payload: dict[str, Any]) -> None:
    loaded = await _load(payload)
    if loaded is None:
        return
    suite, llm = loaded
    try:
        await graph.resume(
            await _deps(suite, llm), suite.agent_thread_id or f"testgen:{suite.id}", payload.get("results") or {}
        )
    except AppError as exc:
        await _fail(suite, exc.message)


# ---------------------------------------------------------- runner job ----
async def _validation_spec(
    session: AsyncSession, jb: ExecutionJob, lease_id: str, runner_id: str, public_key: str
) -> dict[str, Any]:
    suite = await session.get(TestSuite, uuid.UUID(jb.spec["suite_id"]))
    target = await session.get(Target, uuid.UUID(jb.spec["target_id"]))
    if suite is None or target is None or not target.is_runnable:
        raise AppError("CONFLICT", "validation target is no longer verified")
    manifest = json.loads(artifacts.read(await suites._manifest(session, suite)))
    ids = [uuid.UUID(c) for c in jb.spec["case_ids"]]
    rows = (
        await session.execute(
            select(TestCase, Workflow).join(Workflow, Workflow.id == TestCase.workflow_id).where(TestCase.id.in_(ids))
        )
    ).all()
    cases, names = [], set()
    for tc, wf in rows:
        names |= secret_names(wf.spec)
        if tc.type == "k6_scenario":
            cases.append({"id": str(tc.id), "type": tc.type, "exec_name": manifest["exec_names"][wf.key]})
        elif tc.type == "playwright":
            cases.append({"id": str(tc.id), "type": tc.type, "spec": tc.path.split("/", 1)[1]})
    values = await secrets.resolve_for_run(session, Actor("runner", runner_id), jb.org_id, suite.project_id, names)
    repeats = int(jb.spec.get("repeats", 3))
    return {
        "suite_id": str(suite.id),
        "env": {"BASE_URL": target.base_url, "THINK_MIN_S": "0", "THINK_MAX_S": "0"},
        "secrets_sealed": seal_for_x25519(public_key, json.dumps(values).encode()),
        "egress": {"allow_hosts": [target.host], "allow_ips": [str(ip) for ip in target.resolved_ips or []]},
        "bundles": {
            p: artifacts.download_url(await suites.bundle_artifact(session, suite, p), ttl=3600)
            for p in suites.BUNDLE_PARTS
        },
        "cases": cases,
        "repeats": repeats,
        "max_wall_s": min(3600, SECONDS_PER_DRY_RUN * max(1, len(cases)) * repeats),
        "limits": LIMITS["functional"],
        "uploads": {},
    }


async def _validation_done(session: AsyncSession, jb: ExecutionJob, body: dict[str, Any]) -> None:
    results = ((body.get("result") or {}).get("validation")) or {}
    if body.get("outcome") == "error" or not results:
        reason = body.get("abort_reason") or "validation job failed"
        results = {c: [{"exit_code": -1, "log_tail": reason}] for c in jb.spec.get("case_ids", [])}
    await outbox.enqueue(
        session,
        "testsuites.validated",
        {"suite_id": jb.spec["suite_id"], "org_id": jb.org_id, "results": results},
        org_id=jb.org_id,
    )


runners.register_job_type("test_validation", runners.JobType(_validation_spec, _validation_done))
