"""Runs the diagnosis graph in an ``agents`` worker and wires its experiment (two short system
load runs in the same sandbox: 1 VU vs the observed load) back into the waiting thread."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from reliability.agents.common.budget import GraphBudget
from reliability.agents.diagnosis import graph
from reliability.agents.diagnosis.citations import Evidence
from reliability.agents.diagnosis.code import CodeIndex
from reliability.agents.discovery.graph import path_shape
from reliability.agents.discovery.snapshot import from_archive
from reliability.llm import get_gateway
from reliability.modules.artifacts import service as artifacts
from reliability.modules.artifacts.models import Artifact
from reliability.modules.diagnosis import service
from reliability.modules.diagnosis.models import Diagnosis
from reliability.modules.discovery.models import AgentThread
from reliability.modules.findings import service as findings
from reliability.modules.findings.detectors import EvidenceSpec
from reliability.modules.findings.models import Evidence as EvidenceRow
from reliability.modules.findings.models import Finding
from reliability.modules.integrations.github import service as github
from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.projects.models import Project
from reliability.modules.quotas.llm_sink import LedgerUsageSink
from reliability.modules.runs import jobs as run_jobs
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import TERMINAL_STATUSES, Run, RunMetricSummary
from reliability.modules.runs.timeseries import TimeSeries
from reliability.modules.tenancy.models import Organization
from reliability.modules.testsuites import service as suites
from reliability.modules.workflows.ir import api_steps
from reliability.modules.workflows.models import Workflow
from reliability.platform import outbox
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.flags import is_enabled
from reliability.platform.ids import uuid7
from reliability.platform.jobs import job

log = logging.getLogger(__name__)
LLM_DISABLED_FLAG = "agents.diagnosis.llm_disabled"
EXPERIMENTS_DISABLED_FLAG = "agents.diagnosis.experiments_disabled"
EXPERIMENT_DURATION_S = 20
EXPERIMENT_RAMP_S = 2
EXPERIMENT_DEADLINE = timedelta(minutes=30)
CHECK_DELAY_S = 30

run_jobs.POST_ANALYSIS_TOPICS.append("diagnosis.experiment_run_done")


def _f(v: Decimal | float | None) -> float | None:
    return None if v is None else float(v)


def _probe_endpoint(spec: dict[str, Any], invariant_id: str) -> str | None:
    """The request a concurrency probe fires in parallel — the handler to analyse for a race."""
    inv = next((i for i in spec.get("invariants") or [] if i.get("id") == invariant_id), None)
    step = next((st for st in api_steps(spec) if inv and st.get("id") == inv.get("step_ref")), None)
    return f"{step['request']['method']} {step['request']['path']}" if step else None


class Ports:
    def __init__(self, diag: Diagnosis, finding: Finding) -> None:
        self.diag_id, self.org_id, self.finding_id = diag.id, diag.org_id, finding.id
        self.project_id, self.run_id = finding.project_id, finding.last_run_id
        self._finding: dict[str, Any] | None = None
        self._run: dict[str, Any] | None = None
        self._code: CodeIndex | None = None
        self._code_loaded = False

    def _tx(self) -> Any:
        return tenant_tx(org_ids=[self.org_id])

    async def finding(self) -> dict[str, Any]:
        if self._finding is None:
            async with self._tx() as s:
                f = await s.get(Finding, self.finding_id)
                assert f is not None
                wf = await s.get(Workflow, f.workflow_id) if f.workflow_id else None
                self._finding = {
                    "id": str(f.id),
                    "type": f.type,
                    "severity": f.severity,
                    "title": f.title,
                    "headline": f.headline,
                    "run_id": str(f.last_run_id),
                    "workflow_key": wf.key if wf else None,
                    "occurrences": f.occurrences,
                }
                if wf is not None and f.type == "invariant_violation":
                    self._finding["endpoint"] = _probe_endpoint(wf.spec, str(f.headline.get("invariant") or ""))
        return self._finding

    async def existing_evidence(self) -> list[Evidence]:
        async with self._tx() as s:
            rows = await findings.evidence_for(s, self.finding_id)
            return [
                Evidence(str(e.id), e.kind, e.summary, e.locator, e.snapshot)
                for e in rows
                if e.collected_by in ("detector", "user") or str(e.run_id) == str(self.run_id)
            ]

    async def run_data(self) -> dict[str, Any]:
        if self._run is not None:
            return self._run
        async with self._tx() as s:
            run = await s.get(Run, self.run_id)
            assert run is not None
            rows = list((await s.execute(select(RunMetricSummary).where(RunMetricSummary.run_id == run.id))).scalars())
            arts = await artifacts.for_run(s, run.id)
            ts_art = next((a for a in arts if a.kind == "k6_timeseries"), None)
            ts = TimeSeries(json.loads(artifacts.read(ts_art))) if ts_art else None
        window = "steady_state" if any(r.time_window == "steady_state" for r in rows) else "full"
        scopes = {
            r.scope: {
                "requests": int(r.requests),
                "rps": _f(r.rps),
                "error_rate": _f(r.error_rate),
                "p50_ms": _f(r.p50_ms),
                "p95_ms": _f(r.p95_ms),
                "p99_ms": _f(r.p99_ms),
            }
            for r in rows
            if r.time_window == window
        }
        self._run = {
            "id": str(run.id),
            "kind": run.kind,
            "environment_kind": run.environment_kind,
            "profile": (run.config_snapshot or {}).get("profile"),
            "overall": scopes.get("overall") or {},
            "endpoints": {k: v for k, v in scopes.items() if k.startswith("endpoint:")},
            "series": {k: ts.series(k)[:120] for k in scopes} if ts else {},
            "samples": ts.samples if ts else {},
            "sandbox_report": (run.build_ref or {}).get("report") if run.environment_kind == "sandbox_build" else None,
            "build_ref": {k: v for k, v in (run.build_ref or {}).items() if k in ("archive_artifact_id", "commit_sha")},
            "suite_id": str(run.suite_id) if run.suite_id else None,
        }
        return self._run

    async def code(self) -> CodeIndex | None:
        """Source at the run's commit: the sandbox archive, else the repository default branch
        (noted: may differ from what was deployed), else the latest uploaded archive."""
        if self._code_loaded:
            return self._code
        run = await self.run_data()
        async with self._tx() as s:
            archive_id = (run.get("build_ref") or {}).get("archive_artifact_id")
            art = await s.get(Artifact, uuid.UUID(archive_id)) if archive_id else None
            commit = (run.get("build_ref") or {}).get("commit_sha")
            data: bytes | None = None
            if art is not None and art.expires_at > datetime.now(UTC):
                data = artifacts.read(art)
            else:
                repo = await github.repository_for_project(s, self.project_id)
                if repo is not None:
                    try:
                        commit, data = await github.snapshot(s, repo, repo.default_branch)
                    except AppError:
                        data = None
                if data is None:
                    latest = (
                        await s.execute(
                            select(Artifact)
                            .where(
                                Artifact.project_id == self.project_id,
                                Artifact.kind == "code_snapshot",
                                Artifact.expires_at > datetime.now(UTC),
                            )
                            .order_by(Artifact.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    data = artifacts.read(latest) if latest is not None else None
        self._code = CodeIndex.build(from_archive(data), commit) if data else None
        self._code_loaded = True
        return self._code

    async def add_evidence(
        self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
    ) -> Evidence:
        async with self._tx() as s:
            f = await s.get(Finding, self.finding_id)
            row = await findings.add_evidence(
                s,
                f,
                uuid.UUID(str(self.run_id)),
                EvidenceSpec(kind, {**locator, "diagnosis_id": str(self.diag_id)}, summary, snapshot),
                "experiment" if kind == "experiment" else "diagnosis_tool",
            )
            return Evidence(str(row.id), row.kind, row.summary, row.locator, row.snapshot)

    async def progress(self, status: str) -> None:
        async with self._tx() as s:
            d = await s.get(Diagnosis, self.diag_id, with_for_update=True)
            if d is not None and d.status in service.ACTIVE:
                d.status = status

    async def start_experiment(self, endpoint: str, vus: int) -> dict[str, Any] | None:
        run_info = await self.run_data()
        archive = (run_info.get("build_ref") or {}).get("archive_artifact_id")
        if run_info["environment_kind"] != "sandbox_build" or not archive or not run_info.get("suite_id"):
            return None  # never send extra traffic to a customer target without approval
        async with self._tx() as s:
            if await is_enabled(s, EXPERIMENTS_DISABLED_FLAG, self.org_id):
                return None
            project = await s.get(Project, self.project_id)
            suite = await suites.get(s, uuid.UUID(run_info["suite_id"]))
            wfs = [
                w
                for c, w in await suites.cases(s, suite.id)
                if c.type == "k6_scenario"
                and any(
                    f"{st['request']['method']} {path_shape(st['request']['path'])}"
                    == f"{endpoint.split(' ', 1)[0]} {path_shape(endpoint.split(' ', 1)[1])}"
                    for st in api_steps(w.spec)
                )
            ]
            if project is None or not wfs:
                return None
            ids = []
            for level in (1, vus):
                profile = LoadProfile(
                    id=uuid7(),
                    org_id=self.org_id,
                    project_id=self.project_id,
                    name=f"experiment {level} VU",
                    type="load",
                    executor="ramping-vus",
                    # Ramp quickly, then hold: a single 0→1 ramp only starts its VU at the very end.
                    stages=[
                        {"duration_s": EXPERIMENT_RAMP_S, "target": level},
                        {"duration_s": EXPERIMENT_DURATION_S, "target": level},
                    ],
                    max_vus=level,
                    duration_s=EXPERIMENT_RAMP_S + EXPERIMENT_DURATION_S,
                    think_time_min_s=Decimal("0"),
                    think_time_max_s=Decimal("0.05"),
                    workload_mix={w.key: 1.0 for w in wfs},
                    thresholds={"p95_ms": 100000, "error_rate": 1.0, "abort_error_rate": 1.0},
                )
                run = await runs.create_run(
                    s,
                    None,
                    project,
                    kind="load",
                    suite_id=suite.id,
                    load_profile_id=None,
                    target_id=None,
                    environment_kind="sandbox_build",
                    build_ref={"archive_artifact_id": archive},
                    system=True,
                    requested_via="system",
                    profile_override=profile,
                    config_extra={"experiment": {"diagnosis_id": str(self.diag_id), "level": level}},
                    notes=f"diagnosis experiment: {endpoint} at {level} VU",
                )
                ids.append(str(run.id))
            thread = await s.get(AgentThread, f"diagnosis:{self.diag_id}")
            if thread is not None:
                thread.pending_interrupt = {
                    "experiment_runs": ids,
                    "endpoint": endpoint,
                    "vus": vus,
                    "deadline": (datetime.now(UTC) + EXPERIMENT_DEADLINE).isoformat(),
                }
            await outbox.enqueue(
                s,
                "diagnosis.experiment_check",
                {"diagnosis_id": self.diag_id, "org_id": self.org_id},
                org_id=self.org_id,
                delay_seconds=CHECK_DELAY_S,
            )
            return {"runs": ids, "vus": vus}

    async def persist(self, result: dict[str, Any]) -> None:
        async with self._tx() as s:
            d = await s.get(Diagnosis, self.diag_id, with_for_update=True)
            assert d is not None
            await service.save_result(s, d, result)
            thread = await s.get(AgentThread, f"diagnosis:{self.diag_id}")
            if thread is not None:
                thread.pending_interrupt = None


async def _deps(diag_id: uuid.UUID, org_id: uuid.UUID) -> graph.DiagnosisDeps | None:
    async with tenant_tx(org_ids=[org_id]) as s:
        d = await s.get(Diagnosis, diag_id)
        if d is None or d.status not in service.ACTIVE:
            return None
        f = await s.get(Finding, d.finding_id)
        org = await s.get(Organization, org_id)
        assert f is not None
        code_ok = bool(org and org.llm_code_context_enabled)
        llm_ok = not await is_enabled(s, LLM_DISABLED_FLAG, org_id)
        experiments_ok = not await is_enabled(s, EXPERIMENTS_DISABLED_FLAG, org_id)
        ports = Ports(d, f)
    return graph.DiagnosisDeps(
        ports,
        get_gateway(),
        LedgerUsageSink(),
        GraphBudget.for_graph("diagnosis"),
        org_id,
        diag_id,
        llm_enabled=llm_ok,
        code_enabled=code_ok,
        experiments_enabled=experiments_ok,
    )


async def _fail(diag_id: uuid.UUID, org_id: uuid.UUID, message: str) -> None:
    async with tenant_tx(org_ids=[org_id]) as s:
        d = await s.get(Diagnosis, diag_id, with_for_update=True)
        if d is not None and d.status in service.ACTIVE:
            d.status, d.finished_at = "failed", datetime.now(UTC)
            d.model_info = {**(d.model_info or {}), "error": message[:500]}
            f = await s.get(Finding, d.finding_id)
            if f is not None and f.status == "diagnosing":
                f.status = "open"


@job("diagnosis.run", queue="agents", max_retries=2)
async def run_diagnosis(payload: dict[str, Any]) -> None:
    diag_id, org_id = uuid.UUID(payload["diagnosis_id"]), uuid.UUID(payload["org_id"])
    deps = await _deps(diag_id, org_id)
    if deps is None:
        return
    try:
        await graph.start(deps, f"diagnosis:{diag_id}")
    except AppError as exc:
        await _fail(diag_id, org_id, exc.message)
    except Exception as exc:
        log.exception("diagnosis failed", extra={"diagnosis_id": str(diag_id)})
        await _fail(diag_id, org_id, type(exc).__name__)


async def _experiment_results(org_id: uuid.UUID, pending: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """(done, results). Results are None when the experiment could not complete."""
    ids = [uuid.UUID(i) for i in pending.get("experiment_runs", [])]
    async with tenant_tx(org_ids=[org_id]) as s:
        rows = list((await s.execute(select(Run).where(Run.id.in_(ids)))).scalars())
        if len(rows) != len(ids):
            return True, None
        if not all(r.status in TERMINAL_STATUSES for r in rows):
            if datetime.now(UTC) > datetime.fromisoformat(pending["deadline"]):
                return True, None
            return False, None
        if any(r.status not in ("passed", "failed") for r in rows):
            return True, None
        endpoint = pending["endpoint"]
        out: dict[str, Any] = {"vus": pending.get("vus")}
        for r in rows:
            level = (r.config_snapshot.get("experiment") or {}).get("level")
            summaries = list(
                (await s.execute(select(RunMetricSummary).where(RunMetricSummary.run_id == r.id))).scalars()
            )
            row = next(
                (
                    m
                    for m in summaries
                    if m.scope.startswith("endpoint:")
                    and path_shape(m.scope.split(" ", 1)[-1]) == path_shape(endpoint.split(" ", 1)[1])
                    and m.scope.split(":", 1)[1].split(" ", 1)[0] == endpoint.split(" ", 1)[0]
                    and m.time_window == "full"
                ),
                None,
            )
            if row is None or not row.requests:
                return True, None
            out["low" if level == 1 else "high"] = {
                "p95_ms": _f(row.p95_ms),
                "error_rate": _f(row.error_rate),
                "requests": int(row.requests),
                "run_id": str(r.id),
            }
        return True, out


async def _check(diag_id: uuid.UUID, org_id: uuid.UUID) -> None:
    async with tenant_tx(org_ids=[org_id]) as s:
        thread = await s.get(AgentThread, f"diagnosis:{diag_id}")
        pending = dict(thread.pending_interrupt or {}) if thread else {}
    if not pending.get("experiment_runs"):
        return
    done, results = await _experiment_results(org_id, pending)
    if not done:
        async with tenant_tx(org_ids=[org_id]) as s:
            await outbox.enqueue(
                s,
                "diagnosis.experiment_check",
                {"diagnosis_id": diag_id, "org_id": org_id},
                org_id=org_id,
                delay_seconds=CHECK_DELAY_S,
            )
        return
    async with tenant_tx(org_ids=[org_id]) as s:  # claim the resume exactly once
        thread = await s.get(AgentThread, f"diagnosis:{diag_id}", with_for_update=True)
        if thread is None or not (thread.pending_interrupt or {}).get("experiment_runs"):
            return
        thread.pending_interrupt = {"resumed": True}
    deps = await _deps(diag_id, org_id)
    if deps is None:
        return
    try:
        await graph.resume(deps, f"diagnosis:{diag_id}", results)
    except Exception as exc:
        log.exception("diagnosis resume failed", extra={"diagnosis_id": str(diag_id)})
        await _fail(diag_id, org_id, type(exc).__name__)


@job("diagnosis.experiment_check", queue="agents")
async def experiment_check(payload: dict[str, Any]) -> None:
    await _check(uuid.UUID(payload["diagnosis_id"]), uuid.UUID(payload["org_id"]))


@job("diagnosis.experiment_run_done", queue="agents")
async def experiment_run_done(payload: dict[str, Any]) -> None:
    org_id, run_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["run_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(Run, run_id)
        exp = (run.config_snapshot or {}).get("experiment") if run else None
    if exp:
        await _check(uuid.UUID(exp["diagnosis_id"]), org_id)


__all__ = ["EvidenceRow", "experiment_check", "run_diagnosis"]
