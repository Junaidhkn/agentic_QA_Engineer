"""Repair jobs: run/resume the repair graph, react to verification results, open approved PRs.

The graph's ports are implemented here: code comes from the frozen sandbox archive, checks run
as ``repair_check`` sandbox jobs whose completion resumes the waiting thread, and the diff
policy is the verification module's own (the agent cannot import verification directly).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from reliability.agents.common.budget import GraphBudget
from reliability.agents.discovery.snapshot import CodeSnapshot, from_archive
from reliability.agents.repair import graph
from reliability.llm import get_gateway
from reliability.modules.artifacts import service as artifacts
from reliability.modules.discovery.models import AgentThread
from reliability.modules.environments import checks as sandbox_checks
from reliability.modules.environments.source import overlay_archive
from reliability.modules.findings.models import Evidence, Finding
from reliability.modules.quotas.llm_sink import LedgerUsageSink
from reliability.modules.repairs import service
from reliability.modules.repairs.models import RepairAttempt
from reliability.modules.runs.models import ExecutionJob
from reliability.modules.verification import policy
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.flags import is_enabled
from reliability.platform.jobs import job

log = logging.getLogger(__name__)
LLM_DISABLED_FLAG = "agents.repair.llm_disabled"


class Ports:
    def __init__(self, attempt: RepairAttempt) -> None:
        self.attempt_id, self.org_id = attempt.id, attempt.org_id
        self.constraints = dict(attempt.constraints)
        self.ctx = dict(attempt.context)
        self._snapshot: CodeSnapshot | None = None
        self._base: bytes | None = None

    def _tx(self) -> Any:
        return tenant_tx(org_ids=[self.org_id])

    async def _base_bytes(self) -> bytes:
        if self._base is None:
            async with self._tx() as s:
                art = await artifacts.get(s, uuid.UUID(self.ctx["base_archive_id"]))
                self._base = artifacts.read(art)
        return self._base

    async def context(self) -> dict[str, Any]:
        async with self._tx() as s:
            attempt = await service.get(s, self.attempt_id)
            return await service.context(s, attempt)

    async def snapshot(self) -> CodeSnapshot:
        if self._snapshot is None:
            self._snapshot = from_archive(await self._base_bytes())
        return self._snapshot

    def path_policy(self, path: str) -> str | None:
        return policy.path_violation(path, self.constraints)

    def diff_policy(self, changes: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]:
        return policy.check(changes, self.constraints).to_dict()

    async def start_checks(
        self, stage: str, files: dict[str, str | None], checks: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        data = overlay_archive(await self._base_bytes(), files)
        async with self._tx() as s:
            attempt = await service.get(s, self.attempt_id)
            art = await artifacts.put(
                s,
                org_id=self.org_id,
                project_id=attempt.project_id,
                kind="code_snapshot",
                owner_id=self.attempt_id,
                name=f"check-{stage}-{uuid.uuid4().hex[:12]}.tar.gz",
                data=data,
                content_type="application/gzip",
                repair_attempt_id=self.attempt_id,
            )
            jb = await sandbox_checks.enqueue(
                s,
                org_id=self.org_id,
                project_id=attempt.project_id,
                subject_id=self.attempt_id,
                archive_artifact_id=str(art.id),
                plan=self.ctx.get("plan") or {},
                checks=[sandbox_checks.Check(c["id"], list(c["argv"]), int(c.get("repeat", 1))) for c in checks],
                scan=False,
                notify_topic="repair.check_done",
                notify_payload={"attempt_id": str(self.attempt_id), "org_id": str(self.org_id), "stage": stage},
            )
            thread = await s.get(AgentThread, f"repair:{self.attempt_id}")
            if thread is not None:
                thread.pending_interrupt = {"stage": stage, "job_id": str(jb.id)}
            return {"job_id": str(jb.id)}

    async def evidence(self, evidence_id: str) -> str | None:
        try:
            eid = uuid.UUID(evidence_id)
        except ValueError:
            return None
        async with self._tx() as s:
            attempt = await service.get(s, self.attempt_id)
            ev = await s.get(Evidence, eid)
            if ev is None or ev.finding_id != attempt.finding_id:
                return None
            return json.dumps(
                {"kind": ev.kind, "summary": ev.summary, "locator": ev.locator, "snapshot": ev.snapshot}, default=str
            )[:20000]

    async def progress(self, status: str) -> None:
        async with self._tx() as s:
            attempt = await s.get(RepairAttempt, self.attempt_id, with_for_update=True)
            if attempt is not None and attempt.status in ("queued", "planning", "coding"):
                service.set_status(attempt, status)

    async def finish(self, result: dict[str, Any]) -> None:
        async with self._tx() as s:
            attempt = await s.get(RepairAttempt, self.attempt_id, with_for_update=True)
            if attempt is None or attempt.status not in ("queued", "planning", "coding"):
                return
            thread = await s.get(AgentThread, f"repair:{self.attempt_id}")
            if thread is not None:
                thread.pending_interrupt = None
            attempt.token_usage = {"tokens": (result.get("model_info") or {}).get("tokens")}
            if result.get("status") == "candidate":
                await service.submit_candidate(s, attempt, result)
            else:
                service.set_status(attempt, str(result.get("status", "failed")), str(result.get("reason", ""))[:1000])
                finding = await s.get(Finding, attempt.finding_id)
                if finding is not None and finding.status == "fixing":
                    finding.status = "diagnosed"


async def _deps(attempt_id: uuid.UUID, org_id: uuid.UUID) -> graph.RepairDeps | None:
    async with tenant_tx(org_ids=[org_id]) as s:
        attempt = await s.get(RepairAttempt, attempt_id)
        if attempt is None or attempt.status not in ("queued", "planning", "coding"):
            return None
        llm_ok = not await is_enabled(s, LLM_DISABLED_FLAG, org_id)
        ports = Ports(attempt)
    return graph.RepairDeps(
        ports,
        get_gateway(),
        LedgerUsageSink(),
        GraphBudget.for_graph("repair"),
        org_id,
        attempt_id,
        llm_enabled=llm_ok,
    )


async def _fail(attempt_id: uuid.UUID, org_id: uuid.UUID, message: str) -> None:
    async with tenant_tx(org_ids=[org_id]) as s:
        attempt = await s.get(RepairAttempt, attempt_id, with_for_update=True)
        if attempt is not None and attempt.status in ("queued", "planning", "coding"):
            service.set_status(attempt, "failed", message[:1000])


@job("repair.run", queue="agents", max_retries=1)
async def run_repair(payload: dict[str, Any]) -> None:
    attempt_id, org_id = uuid.UUID(payload["attempt_id"]), uuid.UUID(payload["org_id"])
    deps = await _deps(attempt_id, org_id)
    if deps is None:
        return
    try:
        await graph.start(deps, f"repair:{attempt_id}")
    except AppError as exc:
        await _fail(attempt_id, org_id, exc.message)
    except Exception as exc:
        log.exception("repair failed", extra={"repair_attempt_id": str(attempt_id)})
        await _fail(attempt_id, org_id, type(exc).__name__)


@job("repair.check_done", queue="agents", max_retries=1)
async def check_done(payload: dict[str, Any]) -> None:
    attempt_id, org_id = uuid.UUID(payload["attempt_id"]), uuid.UUID(payload["org_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        thread = await s.get(AgentThread, f"repair:{attempt_id}")
        if thread is None or (thread.pending_interrupt or {}).get("job_id") != payload["job_id"]:
            return  # stale or duplicate notification
        jb = await s.get(ExecutionJob, uuid.UUID(payload["job_id"]))
        if jb is None:
            return
        out = sandbox_checks.outcome(jb)
        thread.pending_interrupt = None
    deps = await _deps(attempt_id, org_id)
    if deps is None:
        return
    outcome = {"built": out.built, "error": out.error, "checks": out.checks}
    try:
        await graph.resume(deps, f"repair:{attempt_id}", outcome)
    except AppError as exc:
        await _fail(attempt_id, org_id, exc.message)
    except Exception as exc:
        log.exception("repair failed", extra={"repair_attempt_id": str(attempt_id)})
        await _fail(attempt_id, org_id, type(exc).__name__)


@job("repairs.verification_done")
async def verification_done(payload: dict[str, Any]) -> None:
    attempt_id, org_id = uuid.UUID(payload["attempt_id"]), uuid.UUID(payload["org_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        attempt = await s.get(RepairAttempt, attempt_id, with_for_update=True)
        if attempt is not None:
            await service.on_verification_done(s, attempt)


@job("repairs.open_pr", queue="github")
async def open_pr(payload: dict[str, Any]) -> None:
    attempt_id, org_id = uuid.UUID(payload["attempt_id"]), uuid.UUID(payload["org_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        attempt = await s.get(RepairAttempt, attempt_id, with_for_update=True)
        if attempt is not None:
            await service.open_pr(s, attempt)
