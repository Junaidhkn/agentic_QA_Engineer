"""Verification pipeline (low-level-design §9, ADR-0016, ADR-0007).

Deterministic and independent of the repair agent: it receives frozen inputs (base archive,
the patch, the regression test and the commands to run) and decides ``verified`` only when
every applicable gate passed.

1. ``diff_policy`` is decided immediately from the patch.
2. Two ``repair_check`` sandbox jobs run in parallel — base+test and base+test+patch — giving
   ``build``, ``existing_tests``, ``fail_to_pass`` (3 runs each side) and the scanner outputs
   for ``security``.
3. Runtime gates then run one sandbox run at a time so runs never compete for the same host:
   ``functional_suite`` (approved suite on the patched build), ``invariant_probe`` (5 probe
   runs, invariant findings) and ``load_comparison`` (interleaved B P B P B P load runs,
   performance findings) with the BCa comparison against the project's noise floor.

The first failed gate stops the pipeline; remaining gates are recorded as skipped. State lives
in ``verifications`` / ``verification_gates``; :func:`advance` is idempotent and is called
whenever a check job or run finishes.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.environments import checks as sandbox_checks
from reliability.modules.environments import service as environments
from reliability.modules.environments.source import overlay_archive
from reliability.modules.loadprofiles.models import LoadProfile
from reliability.modules.projects.models import Project
from reliability.modules.runs import service as runs
from reliability.modules.runs.models import TERMINAL_STATUSES, ExecutionJob, Run
from reliability.modules.runs.timeseries import TimeSeries, Window
from reliability.modules.verification import policy, security, stats
from reliability.modules.verification.models import PerformanceComparison, Verification, VerificationGate
from reliability.platform import outbox
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.settings import get_settings

GATES = (
    "build",
    "existing_tests",
    "fail_to_pass",
    "functional_suite",
    "security",
    "load_comparison",
    "invariant_probe",
    "diff_policy",
)
CODE_GATES = ("build", "existing_tests", "fail_to_pass", "security")
RUNTIME_GATES = ("functional_suite", "invariant_probe", "load_comparison")
LATENCY_FINDINGS = {"slo_breach", "regression", "capacity"}
ERROR_FINDINGS = {"error_spike"}
PERFORMANCE_FINDINGS = LATENCY_FINDINGS | ERROR_FINDINGS
TERMINAL_GATE = {"passed", "failed", "skipped", "errored"}
FAILURE_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
FAIL_TO_PASS_RUNS = 3
RAMP_S = 5
DONE_TOPIC = "repairs.verification_done"
NOISE_FLOOR_MAX_AGE = timedelta(days=30)


# ------------------------------------------------------------- inputs ----
def changes_of(inputs: dict[str, Any], *, include_tests: bool = True) -> dict[str, tuple[str | None, str | None]]:
    out = {p: (v[0], v[1]) for p, v in (inputs.get("changes") or {}).items()}
    if include_tests:
        out.update({p: (v[0], v[1]) for p, v in (inputs.get("test_changes") or {}).items()})
    return out


def _new_files(changes: dict[str, tuple[str | None, str | None]]) -> dict[str, str | None]:
    return {p: new for p, (_, new) in changes.items()}


def applicable(inputs: dict[str, Any]) -> dict[str, bool]:
    finding = inputs.get("finding") or {}
    return {
        "build": True,
        "existing_tests": bool(inputs.get("existing_argv")),
        "fail_to_pass": True,
        "functional_suite": bool(inputs.get("suite_id")),
        "security": True,
        "load_comparison": finding.get("type") in PERFORMANCE_FINDINGS
        and bool(finding.get("endpoint"))
        and bool((inputs.get("load") or {}).get("workflow_keys")),
        "invariant_probe": finding.get("type") == "invariant_violation",
        "diff_policy": True,
    }


# -------------------------------------------------------------- start ----
async def start(
    session: AsyncSession, *, org_id: uuid.UUID, project_id: uuid.UUID, attempt_id: uuid.UUID, inputs: dict[str, Any]
) -> Verification:
    existing = (
        await session.execute(select(Verification).where(Verification.repair_attempt_id == attempt_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    v = Verification(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        repair_attempt_id=attempt_id,
        status="running",
        inputs=inputs,
        archives={},
    )
    session.add(v)
    now = datetime.now(UTC)
    flags = applicable(inputs)
    gates = {
        name: VerificationGate(
            id=uuid7(),
            org_id=org_id,
            repair_attempt_id=attempt_id,
            gate=name,
            status="pending" if flags[name] else "skipped",
            applicable=flags[name],
            run_ids=[],
            result=None if flags[name] else {"reason": _not_applicable_reason(name, inputs)},
        )
        for name in GATES
    }
    session.add_all(gates.values())
    verdict = policy.check(changes_of(inputs), inputs.get("constraints"))
    _finish_gate(gates["diff_policy"], "passed" if verdict.passed else "failed", verdict.to_dict(), now)
    await session.flush()
    if not verdict.passed:
        await _finalize(session, v, list(gates.values()))
        return v

    base_art = await artifacts.get(session, uuid.UUID(inputs["base_archive_id"]))
    base_bytes = artifacts.read(base_art)
    variants = {
        "base": _new_files(changes_of({"test_changes": inputs.get("test_changes")})),
        "patched": _new_files(changes_of(inputs)),
    }
    archives: dict[str, Any] = {}
    for variant, files in variants.items():
        art = await artifacts.put(
            session,
            org_id=org_id,
            project_id=project_id,
            kind="code_snapshot",
            owner_id=v.id,
            name=f"{variant}.tar.gz",
            data=overlay_archive(base_bytes, files),
            content_type="application/gzip",
            repair_attempt_id=attempt_id,
        )
        _, _, plan = await environments.make_plan(
            session, org_id, project_id, {"type": "archive", "archive_artifact_id": str(art.id)}
        )
        if plan.get("unsupported"):
            raise AppError("CONFLICT", f"sandbox cannot be built: {plan['unsupported']}")
        checks = [sandbox_checks.Check("regression", list(inputs["regression_argv"]), FAIL_TO_PASS_RUNS)]
        if inputs.get("existing_argv"):
            checks.insert(0, sandbox_checks.Check("existing_tests", list(inputs["existing_argv"])))
        job = await sandbox_checks.enqueue(
            session,
            org_id=org_id,
            project_id=project_id,
            subject_id=v.id,
            archive_artifact_id=str(art.id),
            plan=plan,
            checks=checks,
            scan=True,
            notify_topic="verification.advance",
            notify_payload={"verification_id": str(v.id), "org_id": str(org_id)},
        )
        archives[variant] = {"artifact_id": str(art.id), "plan": plan, "job_id": str(job.id)}
    v.archives = archives
    for name in CODE_GATES:
        if gates[name].applicable:
            gates[name].status, gates[name].started_at = "running", now
    return v


def _not_applicable_reason(name: str, inputs: dict[str, Any]) -> str:
    return {
        "existing_tests": "the repository has no test command",
        "functional_suite": "the project has no approved functional suite",
        "load_comparison": "not a performance finding (or no load workflow touches the endpoint)",
        "invariant_probe": "not an invariant finding",
    }.get(name, "not applicable")


def _finish_gate(gate: VerificationGate, status: str, result: dict[str, Any], now: datetime | None = None) -> None:
    gate.status, gate.result = status, result
    gate.started_at = gate.started_at or now or datetime.now(UTC)
    gate.finished_at = now or datetime.now(UTC)


# ------------------------------------------------------------ advance ----
async def _gates(session: AsyncSession, attempt_id: uuid.UUID) -> dict[str, VerificationGate]:
    rows = (
        await session.execute(
            select(VerificationGate).where(VerificationGate.repair_attempt_id == attempt_id).with_for_update()
        )
    ).scalars()
    return {g.gate: g for g in rows}


async def advance(session: AsyncSession, verification_id: uuid.UUID) -> None:
    v = await session.get(Verification, verification_id, with_for_update=True)
    if v is None or v.status != "running":
        return
    gates = await _gates(session, v.repair_attempt_id)
    if any(gates[g].status == "running" for g in CODE_GATES):
        jobs = {k: await session.get(ExecutionJob, uuid.UUID(a["job_id"])) for k, a in v.archives.items()}
        outs = {k: sandbox_checks.outcome(j) for k, j in jobs.items() if j is not None}
        if len(outs) < 2 or not all(o.finished for o in outs.values()):
            return
        _evaluate_code_gates(v, gates, outs["base"], outs["patched"])
    if _failed(gates):
        await _finalize(session, v, list(gates.values()))
        return
    for name in RUNTIME_GATES:
        gate = gates[name]
        if not gate.applicable or gate.status in TERMINAL_GATE:
            continue
        if await _step_runtime(session, v, gate) == "waiting":
            return
        if gate.status != "passed":
            break
    await _finalize(session, v, list(gates.values()))


def _failed(gates: dict[str, VerificationGate]) -> bool:
    return any(g.applicable and g.status in ("failed", "errored") for g in gates.values())


def _failures(output: str) -> set[str]:
    return set(FAILURE_LINE.findall(output or ""))


def _evaluate_code_gates(
    v: Verification,
    gates: dict[str, VerificationGate],
    base: sandbox_checks.Outcome,
    patched: sandbox_checks.Outcome,
) -> None:
    now = datetime.now(UTC)
    if not patched.built:
        _finish_gate(gates["build"], "failed", {"error": patched.error, "sandbox": _brief(patched.sandbox)}, now)
    else:
        _finish_gate(gates["build"], "passed", {"sandbox": _brief(patched.sandbox)}, now)

    if gates["existing_tests"].applicable:
        if not patched.built:
            _finish_gate(gates["existing_tests"], "skipped", {"reason": "the patched build failed"}, now)
        else:
            status, result = _existing_tests(base, patched)
            if status == "not_applicable":
                gates["existing_tests"].applicable, status = False, "skipped"
            _finish_gate(gates["existing_tests"], status, result, now)

    if not base.built:
        _finish_gate(gates["fail_to_pass"], "errored", {"reason": f"the base build failed: {base.error}"}, now)
    elif not patched.built:
        _finish_gate(gates["fail_to_pass"], "skipped", {"reason": "the patched build failed"}, now)
    else:
        b, p = base.checks.get("regression", []), patched.checks.get("regression", [])
        base_fails = len(b) == FAIL_TO_PASS_RUNS and all(
            a.get("exit_code") not in (0, None) and not a.get("timed_out") for a in b
        )
        patched_passes = len(p) == FAIL_TO_PASS_RUNS and all(a.get("exit_code") == 0 for a in p)
        result = {
            "base_exit_codes": [a.get("exit_code") for a in b],
            "patched_exit_codes": [a.get("exit_code") for a in p],
            "base_output": (b[-1].get("output") or "")[-1500:] if b else None,
            "patched_output": (p[-1].get("output") or "")[-1500:] if p else None,
            "fails_on_base": base_fails,
            "passes_on_patch": patched_passes,
        }
        _finish_gate(gates["fail_to_pass"], "passed" if base_fails and patched_passes else "failed", result, now)

    ok, sec = security.evaluate(changes_of(v.inputs), base.scan, patched.scan)
    _finish_gate(gates["security"], "passed" if ok else "failed", sec, now)


def _brief(sandbox: dict[str, Any]) -> dict[str, Any]:
    return {k: sandbox.get(k) for k in ("strategy", "driver", "phases", "healthy", "migrations", "error_phase")}


def _existing_tests(base: sandbox_checks.Outcome, patched: sandbox_checks.Outcome) -> tuple[str, dict[str, Any]]:
    p = (patched.checks.get("existing_tests") or [{}])[-1]
    b = (base.checks.get("existing_tests") or [{}])[-1]
    code, base_code = p.get("exit_code"), b.get("exit_code")
    result = {"exit_code": code, "base_exit_code": base_code, "output": (p.get("output") or "")[-2000:]}
    if code == 0:
        return "passed", result
    if code == 5 and base_code == 5:  # pytest: no tests collected
        return "not_applicable", {**result, "reason": "the repository's test command collected no tests"}
    if p.get("refused"):
        return "errored", {**result, "reason": "the runner refused the command"}
    patched_failed, base_failed = _failures(p.get("output", "")), _failures(b.get("output", ""))
    if base_code not in (0, None) and patched_failed and patched_failed <= base_failed:
        return "passed", {**result, "note": "same failures as on the base commit", "failures": sorted(patched_failed)}
    return "failed", {**result, "new_failures": sorted(patched_failed - base_failed)}


# ------------------------------------------------------- runtime gates ----
async def _step_runtime(session: AsyncSession, v: Verification, gate: VerificationGate) -> str:
    """Create the next run of a runtime gate, or evaluate it. Returns 'waiting' or 'done'."""
    finished: list[Run] = []
    for rid in gate.run_ids or []:
        run = await session.get(Run, rid)
        if run is None:
            _finish_gate(gate, "errored", {"reason": "a verification run disappeared"})
            return "done"
        if run.status not in TERMINAL_STATUSES:
            return "waiting"
        finished.append(run)
    if gate.status == "pending":
        gate.status, gate.started_at = "running", datetime.now(UTC)
    s = get_settings()
    if gate.gate == "functional_suite":
        if not finished:
            return await _next_run(session, v, gate, "functional", "patched", 0)
        run = finished[0]
        _finish_gate(
            gate,
            "passed" if run.status == "passed" else ("failed" if run.status == "failed" else "errored"),
            {"run_id": str(run.id), "run_status": run.status, "reason": run.status_reason},
        )
        return "done"
    if gate.gate == "invariant_probe":
        if finished and finished[-1].status != "passed":
            run = finished[-1]
            status = "failed" if run.status == "failed" else "errored"
            _finish_gate(
                gate,
                status,
                {
                    "passed_runs": len(finished) - 1,
                    "of": s.verification_probe_runs,
                    "run_status": run.status,
                    "reason": run.status_reason,
                },
            )
            return "done"
        if len(finished) < s.verification_probe_runs:
            return await _next_run(session, v, gate, "probe", "patched", len(finished))
        _finish_gate(gate, "passed", {"passed_runs": len(finished), "of": s.verification_probe_runs})
        return "done"
    # load_comparison: interleaved B P B P B P
    total = 2 * s.verification_load_runs_per_side
    bad = [r for r in finished if r.status not in ("passed", "failed")]
    if bad:
        _finish_gate(gate, "errored", {"reason": f"load run {bad[0].id} ended {bad[0].status}: {bad[0].status_reason}"})
        return "done"
    if len(finished) < total:
        index = len(finished)
        return await _next_run(session, v, gate, "load", "base" if index % 2 == 0 else "patched", index)
    await _compare_load(session, v, gate, finished)
    return "done"


def _load_profile(v: Verification, gate: VerificationGate) -> LoadProfile:
    s = get_settings()
    load = v.inputs.get("load") or {}
    vus = max(1, min(int(load.get("vus") or 10), 100))
    return LoadProfile(
        id=uuid7(),
        org_id=v.org_id,
        project_id=v.project_id,
        name="verification load comparison",
        type="load",
        executor="ramping-vus",
        stages=[{"duration_s": RAMP_S, "target": vus}, {"duration_s": s.verification_load_hold_s, "target": vus}],
        max_vus=vus,
        duration_s=RAMP_S + s.verification_load_hold_s,
        think_time_min_s=Decimal("0"),
        think_time_max_s=Decimal("0.05"),
        workload_mix={k: 1.0 for k in load.get("workflow_keys") or []},
        thresholds={"p95_ms": 100000, "error_rate": 1.0, "abort_error_rate": 1.0},
    )


async def _next_run(
    session: AsyncSession, v: Verification, gate: VerificationGate, kind: str, variant: str, index: int
) -> str:
    project = await session.get(Project, v.project_id)
    if project is None:
        _finish_gate(gate, "errored", {"reason": "project not found"})
        return "done"
    try:
        run = await runs.create_run(
            session,
            None,
            project,
            kind=kind,
            suite_id=uuid.UUID(v.inputs["suite_id"]) if v.inputs.get("suite_id") else None,
            load_profile_id=None,
            target_id=None,
            environment_kind="sandbox_build",
            build_ref={"archive_artifact_id": v.archives[variant]["artifact_id"]},
            system=True,
            requested_via="system",
            profile_override=_load_profile(v, gate) if kind == "load" else None,
            config_extra={
                "verification": {
                    "verification_id": str(v.id),
                    "gate": gate.gate,
                    "variant": variant,
                    "index": index,
                }
            },
            notes=f"verification {gate.gate}: {variant} run {index + 1}",
        )
    except AppError as exc:
        _finish_gate(gate, "errored", {"reason": f"could not start a verification run: {exc.message}"})
        return "done"
    gate.run_ids = [*(gate.run_ids or []), run.id]
    return "waiting"


async def timeseries(session: AsyncSession, run_id: uuid.UUID) -> TimeSeries | None:
    for a in await artifacts.for_run(session, run_id):
        if a.kind == "k6_timeseries":
            try:
                return TimeSeries(json.loads(artifacts.read(a)))
            except (OSError, ValueError):
                return None
    return None


def run_sample(ts: TimeSeries, scope: str, window: Window) -> stats.RunSample:
    lo, hi = int(window.start_s // ts.bucket_s), int(-(-window.end_s // ts.bucket_s)) - 1
    hist: dict[int, int] = {}
    errors = 0
    for b, entry in ts.scopes.get(scope, {}).items():
        if lo <= b <= hi:
            errors += int(entry.get("errors", 0))
            for k, c in (entry.get("hist") or {}).items():
                hist[int(k)] = hist.get(int(k), 0) + int(c)
    return stats.RunSample(hist, errors)


def comparison_window(run: Run) -> Window:
    stages = ((run.config_snapshot or {}).get("profile") or {}).get("stages") or []
    ramp = int(stages[0]["duration_s"]) if stages else RAMP_S
    hold = int(stages[-1]["duration_s"]) if stages else get_settings().verification_load_hold_s
    settle = min(get_settings().verification_load_settle_s, hold // 3)
    return Window(ramp + settle, ramp + hold)


async def noise_floor(session: AsyncSession, project_id: uuid.UUID) -> float:
    """CI half-width of the project's latest A/A comparison (≤ 30 days old), else 5%."""
    row = (
        await session.execute(
            select(PerformanceComparison)
            .where(
                PerformanceComparison.project_id == project_id,
                PerformanceComparison.repair_attempt_id.is_(None),
                PerformanceComparison.scope == "overall",
                PerformanceComparison.created_at > datetime.now(UTC) - NOISE_FLOOR_MAX_AGE,
            )
            .order_by(PerformanceComparison.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return stats.DEFAULT_NOISE_FLOOR_PCT
    return max(2.0, float(row.ci_high_pct - row.ci_low_pct) / 2)


async def compare_runs(
    session: AsyncSession, baseline: list[Run], candidate: list[Run], scopes: list[str], floor: float
) -> dict[str, stats.Comparison]:
    samples: dict[str, tuple[list[stats.RunSample], list[stats.RunSample]]] = {s: ([], []) for s in scopes}
    params: tuple[float, float] = (0.1, 1.05)
    for side, group in ((0, baseline), (1, candidate)):
        for run in group:
            ts = await timeseries(session, run.id)
            if ts is None:
                raise AppError("CONFLICT", f"run {run.id} has no time series")
            params = (ts.base, ts.growth)
            for scope in scopes:
                samples[scope][side].append(run_sample(ts, scope, comparison_window(run)))
    out = {}
    for scope, (b, c) in samples.items():
        if sum(x.requests for x in b) and sum(x.requests for x in c):
            out[scope] = stats.compare(
                b,
                c,
                noise_floor_pct=floor,
                base_ms=params[0],
                growth=params[1],
                iterations=get_settings().verification_bootstrap_iterations,
            )
    return out


def is_error_finding(finding: dict[str, Any]) -> bool:
    """Judged on errors: error spikes, and SLO breaches whose breached metric is the error rate."""
    return finding.get("type") in ERROR_FINDINGS or finding.get("metric") == "error_rate"


def load_gate(
    finding: dict[str, Any], main: stats.Comparison, overall: stats.Comparison | None
) -> tuple[bool, float, float]:
    """(passed, p95 cost in ms, accepted cost in ms). Latency findings must improve beyond the
    noise floor without more errors; error findings must get significantly fewer errors, and a
    bounded latency cost (a retry, a timeout) is accepted. The whole run must not regress."""
    latency_cost_ms = round(main.candidate_value - main.baseline_value, 2)
    budget_ms = max(get_settings().verification_error_fix_latency_budget_ms, 0.1 * main.baseline_value)
    if is_error_finding(finding):
        passed = main.error_rate_better and (main.verdict != "regressed" or latency_cost_ms <= budget_ms)
    else:
        passed = main.verdict == "improved" and not main.error_rate_worse
    return passed and (overall is None or overall.verdict != "regressed"), latency_cost_ms, budget_ms


async def _compare_load(session: AsyncSession, v: Verification, gate: VerificationGate, finished: list[Run]) -> None:
    endpoint = f"endpoint:{(v.inputs.get('finding') or {}).get('endpoint')}"
    baseline = [r for r in finished if (r.config_snapshot.get("verification") or {}).get("variant") == "base"]
    candidate = [r for r in finished if (r.config_snapshot.get("verification") or {}).get("variant") == "patched"]
    floor = await noise_floor(session, v.project_id)
    try:
        results = await compare_runs(session, baseline, candidate, [endpoint, "overall"], floor)
    except AppError as exc:
        _finish_gate(gate, "errored", {"reason": exc.message})
        return
    for scope, c in results.items():
        session.add(_comparison_row(v.org_id, v.project_id, v.repair_attempt_id, baseline, candidate, scope, c))
    main = results.get(endpoint)
    if main is None:
        _finish_gate(gate, "errored", {"reason": f"no requests reached {endpoint} during the comparison"})
        return
    overall = results.get("overall")
    passed, latency_cost_ms, budget_ms = load_gate(dict(v.inputs.get("finding") or {}), main, overall)
    _finish_gate(
        gate,
        "passed" if passed else "failed",
        {
            "scope": endpoint,
            "verdict": main.verdict,
            "delta_pct": main.delta_pct,
            "ci": [main.ci_low_pct, main.ci_high_pct],
            "noise_floor_pct": floor,
            "error_rate_worse": main.error_rate_worse,
            "error_rate_better": main.error_rate_better,
            "error_rates": [main.baseline_error_rate, main.candidate_error_rate],
            "latency_cost_ms": latency_cost_ms,
            "latency_budget_ms": round(budget_ms, 2),
            "overall_verdict": overall.verdict if overall else None,
            "baseline_runs": [str(r.id) for r in baseline],
            "candidate_runs": [str(r.id) for r in candidate],
        },
    )


def _comparison_row(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    baseline: list[Run],
    candidate: list[Run],
    scope: str,
    c: stats.Comparison,
) -> PerformanceComparison:
    return PerformanceComparison(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        repair_attempt_id=attempt_id,
        baseline_run_ids=[r.id for r in baseline],
        candidate_run_ids=[r.id for r in candidate],
        scope=scope,
        metric="p95_ms",
        baseline_value=Decimal(str(c.baseline_value)),
        candidate_value=Decimal(str(c.candidate_value)),
        delta_pct=Decimal(str(c.delta_pct)),
        ci_low_pct=Decimal(str(c.ci_low_pct)),
        ci_high_pct=Decimal(str(c.ci_high_pct)),
        noise_floor_pct=Decimal(str(c.noise_floor_pct)),
        verdict=c.verdict,
        method=c.method,
        environment_controlled=True,
    )


# ----------------------------------------------------------- finalize ----
async def _finalize(session: AsyncSession, v: Verification, gates: list[VerificationGate]) -> None:
    now = datetime.now(UTC)
    for g in gates:
        if g.applicable and g.status not in TERMINAL_GATE:
            _finish_gate(g, "skipped", {"reason": "not run: an earlier gate failed"}, now)
    failed = [g.gate for g in gates if g.applicable and g.status in ("failed", "errored")]
    not_run = [g.gate for g in gates if g.applicable and g.status == "skipped"]
    verified = all(g.status == "passed" for g in gates if g.applicable)  # ADR-0016: every applicable gate
    v.status = "verified" if verified else "failed"
    v.finished_at = now
    v.summary = {"verified": verified, "failed_gates": failed, "not_run": not_run}
    await outbox.enqueue(
        session,
        DONE_TOPIC,
        {"attempt_id": str(v.repair_attempt_id), "verification_id": str(v.id), "org_id": str(v.org_id)},
        org_id=v.org_id,
    )


# -------------------------------------------------------------- reads ----
async def for_attempt(session: AsyncSession, attempt_id: uuid.UUID) -> dict[str, Any] | None:
    v = (
        await session.execute(select(Verification).where(Verification.repair_attempt_id == attempt_id))
    ).scalar_one_or_none()
    if v is None:
        return None
    gates = (
        await session.execute(select(VerificationGate).where(VerificationGate.repair_attempt_id == attempt_id))
    ).scalars()
    comps = (
        await session.execute(
            select(PerformanceComparison).where(PerformanceComparison.repair_attempt_id == attempt_id)
        )
    ).scalars()
    order = {g: i for i, g in enumerate(GATES)}
    return {
        "id": v.id,
        "status": v.status,
        "summary": v.summary,
        "gates": [
            {
                "gate": g.gate,
                "status": g.status,
                "applicable": g.applicable,
                "run_ids": [str(r) for r in g.run_ids or []],
                "result": g.result,
                "started_at": g.started_at,
                "finished_at": g.finished_at,
            }
            for g in sorted(gates, key=lambda g: order.get(g.gate, 99))
        ],
        "comparisons": [
            {
                "scope": c.scope,
                "metric": c.metric,
                "baseline_value": float(c.baseline_value),
                "candidate_value": float(c.candidate_value),
                "delta_pct": float(c.delta_pct),
                "ci_low_pct": float(c.ci_low_pct),
                "ci_high_pct": float(c.ci_high_pct),
                "noise_floor_pct": float(c.noise_floor_pct),
                "verdict": c.verdict,
                "method": c.method,
                "baseline_run_ids": [str(r) for r in c.baseline_run_ids],
                "candidate_run_ids": [str(r) for r in c.candidate_run_ids],
            }
            for c in comps
        ],
        "archives": {k: a.get("artifact_id") for k, a in (v.archives or {}).items()},
        "created_at": v.created_at,
        "finished_at": v.finished_at,
    }


async def latest_noise_floor(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    floor = await noise_floor(session, project_id)
    row = (
        await session.execute(
            select(PerformanceComparison)
            .where(PerformanceComparison.project_id == project_id, PerformanceComparison.repair_attempt_id.is_(None))
            .order_by(PerformanceComparison.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "noise_floor_pct": floor,
        "measured": row is not None,
        "measured_at": row.created_at if row else None,
        "default": row is None,
    }
