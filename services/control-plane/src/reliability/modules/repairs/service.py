"""Repair attempts (FR-24…27): start from a displayed hypothesis, run the repair agent, hand its
candidate to the independent verification pipeline, ask a person to approve, then open the PR.

Status flow: queued → planning → coding → verifying → (verified →) awaiting_approval →
approved → pr_opened, with failed / budget_exhausted / verification_failed / rejected /
abandoned as terminal alternatives. A verification failure starts the next attempt (≤ 3 per
hypothesis) with the failing gates as feedback.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.approvals import service as approvals
from reliability.modules.approvals.models import Approval
from reliability.modules.artifacts import service as artifacts
from reliability.modules.audit import service as audit
from reliability.modules.diagnosis.models import Diagnosis, Hypothesis, HypothesisEvidence
from reliability.modules.discovery.models import AgentThread
from reliability.modules.environments.source import ARCHIVE_SHA
from reliability.modules.findings.models import Evidence, Finding
from reliability.modules.identity.service import Principal
from reliability.modules.integrations.github import pr as github_pr
from reliability.modules.integrations.github import service as github
from reliability.modules.quotas import service as quotas
from reliability.modules.repairs.models import RepairAttempt
from reliability.modules.runs.models import Run
from reliability.modules.tenancy.policy import require
from reliability.modules.testsuites import service as suites
from reliability.modules.verification import policy
from reliability.modules.verification import service as verification
from reliability.modules.workflows.ir import api_steps
from reliability.platform import outbox
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

MAX_ATTEMPTS = 3
ACTIVE = ("queued", "planning", "coding", "candidate", "verifying")
OPEN = (*ACTIVE, "verified", "awaiting_approval", "approved")
CONSTRAINT_KEYS = {"max_files", "max_changed_lines", "allowed_paths"}


def _shape(path: str) -> str:
    import re

    return re.sub(r"\{\{\s*[a-z_][a-z0-9_]*\s*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}", "{}", path.split("?", 1)[0]).rstrip("/")


async def _verified_hypothesis(session: AsyncSession, hypothesis_id: uuid.UUID) -> Hypothesis:
    h = await session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise AppError("NOT_FOUND", "hypothesis not found")
    ok = await session.scalar(
        select(
            exists().where(
                HypothesisEvidence.hypothesis_id == h.id,
                HypothesisEvidence.relation == "supports",
                HypothesisEvidence.check_result == "verified",
            )
        )
    )
    if not ok:
        raise AppError("CONFLICT", "only hypotheses with verified evidence can be repaired")
    return h


async def _source(session: AsyncSession, finding: Finding) -> dict[str, Any]:
    """The code the repair starts from: the sandbox build of the finding's run."""
    run = await session.get(Run, finding.last_run_id)
    ref = (run.build_ref or {}) if run else {}
    archive = ref.get("archive_artifact_id")
    if run is None or not archive or ref.get("status") != "ready":
        raise AppError(
            "CONFLICT",
            "repairs need a sandbox run of this finding (its build is what gets patched and verified); "
            "re-run the test in a sandbox first",
        )
    try:
        await artifacts.get(session, uuid.UUID(archive))
    except AppError as exc:
        raise AppError("CONFLICT", "the sandbox source archive has expired; re-run the test in a sandbox") from exc
    return {
        "run": run,
        "archive_artifact_id": archive,
        "plan": ref.get("plan") or {},
        "commit_sha": ref.get("commit_sha") or ARCHIVE_SHA,
    }


async def _workload(session: AsyncSession, run: Run, endpoint: str | None) -> list[str]:
    if not endpoint or run.suite_id is None:
        return []
    method, _, path = endpoint.partition(" ")
    keys = []
    for case, wf in await suites.cases(session, run.suite_id):
        if case.type != "k6_scenario":
            continue
        if any(
            st["request"]["method"] == method and _shape(st["request"]["path"]) == _shape(path)
            for st in api_steps(wf.spec)
        ):
            keys.append(wf.key)
    return keys


async def _evidence(session: AsyncSession, hypothesis_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(HypothesisEvidence, Evidence)
            .join(Evidence, Evidence.id == HypothesisEvidence.evidence_id)
            .where(HypothesisEvidence.hypothesis_id == hypothesis_id, HypothesisEvidence.check_result == "verified")
        )
    ).all()
    return [
        {"id": str(e.id), "kind": e.kind, "summary": e.summary, "relation": he.relation, "claim": he.claim}
        for he, e in rows
    ]


def _hypothesis_dict(h: Hypothesis) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "category": h.category,
        "statement": h.statement,
        "mechanism": h.mechanism,
        "fix_direction": h.fix_direction,
        "code_locations": h.code_locations,
        "confidence": h.confidence,
        "confidence_factors": h.confidence_factors,
    }


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "type": f.type,
        "severity": f.severity,
        "title": f.title,
        "headline": f.headline,
    }


async def start(
    session: AsyncSession, principal: Principal, hypothesis_id: uuid.UUID, constraints: dict[str, Any] | None = None
) -> RepairAttempt:
    h = await _verified_hypothesis(session, hypothesis_id)
    diag = await session.get(Diagnosis, h.diagnosis_id)
    finding = await session.get(Finding, diag.finding_id) if diag else None
    if finding is None:
        raise AppError("NOT_FOUND", "finding not found")
    require(principal, finding.org_id, "engineer", project_id=finding.project_id)
    running = (
        await session.execute(
            select(RepairAttempt).where(RepairAttempt.hypothesis_id == h.id, RepairAttempt.status.in_(OPEN))
        )
    ).scalar_one_or_none()
    if running is not None:
        return running
    if principal.user_id is None:
        raise AppError("FORBIDDEN", "repairs are requested by a person (API keys cannot start them)")
    await quotas.require_feature(session, finding.org_id, "repair", "Repair")
    overrides = {k: v for k, v in (constraints or {}).items() if k in CONSTRAINT_KEYS}
    attempt = await _create(session, finding, h, principal.user_id, {**policy.DEFAULT_CONSTRAINTS, **overrides})
    await audit.record(
        session,
        principal.actor,
        "repair.requested",
        org_id=finding.org_id,
        subject_type="finding",
        subject_id=finding.id,
        metadata={"repair_attempt_id": str(attempt.id), "hypothesis_id": str(h.id)},
    )
    return attempt


async def _create(
    session: AsyncSession,
    finding: Finding,
    h: Hypothesis,
    requested_by: uuid.UUID,
    constraints: dict[str, Any],
    feedback: str | None = None,
) -> RepairAttempt:
    used = int(
        await session.scalar(
            select(func.count()).where(RepairAttempt.finding_id == finding.id, RepairAttempt.hypothesis_id == h.id)
        )
        or 0
    )
    if used >= MAX_ATTEMPTS:
        raise AppError("CONFLICT", f"this hypothesis already had {MAX_ATTEMPTS} repair attempts")
    src = await _source(session, finding)
    run: Run = src["run"]
    endpoint = None
    scope = str((finding.headline or {}).get("scope") or "")
    if scope.startswith("endpoint:"):
        endpoint = scope.split(":", 1)[1]
    elif finding.type == "invariant_violation":
        endpoint = None
    vus = int(
        (finding.headline or {}).get("at_vus") or ((run.config_snapshot.get("profile") or {}).get("max_vus")) or 10
    )
    attempt_id = uuid7()
    attempt = RepairAttempt(
        id=attempt_id,
        org_id=finding.org_id,
        project_id=finding.project_id,
        finding_id=finding.id,
        hypothesis_id=h.id,
        attempt_no=used + 1,
        base_commit_sha=src["commit_sha"],
        constraints=constraints,
        status="queued",
        agent_thread_id=f"repair:{attempt_id}",
        requested_by=requested_by,
        context={
            "base_archive_id": src["archive_artifact_id"],
            "plan": src["plan"],
            "plan_root": (src["plan"] or {}).get("root", ""),
            "run_id": str(run.id),
            "suite_id": str(run.suite_id) if run.suite_id else None,
            "endpoint": endpoint,
            "vus": max(1, min(vus, 100)),
            "workflow_keys": await _workload(session, run, endpoint),
            "feedback": feedback,
        },
    )
    session.add(attempt)
    session.add(
        AgentThread(thread_id=attempt.agent_thread_id, org_id=finding.org_id, graph="repair", subject_id=attempt.id)
    )
    await session.flush()
    if finding.status in ("open", "diagnosed"):
        finding.status = "fixing"
    await outbox.enqueue(
        session, "repair.run", {"attempt_id": str(attempt.id), "org_id": str(finding.org_id)}, org_id=finding.org_id
    )
    return attempt


async def get(session: AsyncSession, attempt_id: uuid.UUID) -> RepairAttempt:
    attempt = await session.get(RepairAttempt, attempt_id)
    if attempt is None:
        raise AppError("NOT_FOUND", "repair not found")
    return attempt


async def for_finding(session: AsyncSession, finding_id: uuid.UUID) -> list[RepairAttempt]:
    return list(
        (
            await session.execute(
                select(RepairAttempt)
                .where(RepairAttempt.finding_id == finding_id)
                .order_by(RepairAttempt.created_at.desc())
            )
        ).scalars()
    )


async def context(session: AsyncSession, attempt: RepairAttempt) -> dict[str, Any]:
    """What the agent sees: finding, hypothesis, verified evidence, constraints, feedback."""
    finding = await session.get(Finding, attempt.finding_id)
    h = await session.get(Hypothesis, attempt.hypothesis_id)
    assert finding is not None and h is not None
    return {
        "finding": _finding_dict(finding),
        "hypothesis": _hypothesis_dict(h),
        "evidence": await _evidence(session, h.id),
        "constraints": attempt.constraints,
        "feedback": attempt.context.get("feedback"),
        "plan_root": attempt.context.get("plan_root", ""),
    }


def set_status(attempt: RepairAttempt, status: str, reason: str | None = None) -> None:
    attempt.status, attempt.status_reason = status, reason
    attempt.updated_at = datetime.now(UTC)


async def submit_candidate(session: AsyncSession, attempt: RepairAttempt, result: dict[str, Any]) -> None:
    """The agent's candidate becomes frozen verification inputs (the agent never sees them again)."""
    changes = {p: (v[0], v[1]) for p, v in (result.get("changes") or {}).items()}
    tests = {p: (v[0], v[1]) for p, v in (result.get("test_changes") or {}).items()}
    diff = policy.unified_diff({**changes, **tests})
    art = await artifacts.put(
        session,
        org_id=attempt.org_id,
        project_id=attempt.project_id,
        kind="patch",
        owner_id=attempt.id,
        name="repair.diff",
        data=diff.encode(),
        content_type="text/x-diff",
        repair_attempt_id=attempt.id,
    )
    attempt.patch_artifact_id = art.id
    attempt.regression_test_paths = list(result.get("test_paths") or [])
    attempt.diff_stats = policy.diff_stats({**changes, **tests})
    attempt.agent_summary = (result.get("explanation") or "")[:8000]
    attempt.token_usage = {"tokens": (result.get("model_info") or {}).get("tokens")}
    finding = await session.get(Finding, attempt.finding_id)
    from reliability.agents.discovery.snapshot import from_archive
    from reliability.agents.repair.commands import detect

    snapshot = from_archive(artifacts.read(await artifacts.get(session, uuid.UUID(attempt.context["base_archive_id"]))))
    commands = detect(snapshot, attempt.context.get("plan_root", ""))
    if commands is None:
        set_status(attempt, "failed", "no supported test runner found")
        return
    test_paths = sorted(tests)
    inputs = {
        "base_archive_id": attempt.context["base_archive_id"],
        "changes": {p: list(v) for p, v in changes.items()},
        "test_changes": {p: list(v) for p, v in tests.items()},
        "regression_argv": commands.regression(test_paths),
        "existing_argv": commands.existing_without(test_paths),
        "constraints": attempt.constraints,
        "finding": {
            "type": finding.type if finding else None,
            "metric": (finding.headline or {}).get("metric") if finding else None,
            "endpoint": attempt.context.get("endpoint"),
        },
        "suite_id": attempt.context.get("suite_id"),
        "load": {"vus": attempt.context.get("vus"), "workflow_keys": attempt.context.get("workflow_keys") or []},
    }
    set_status(attempt, "verifying")
    try:
        await verification.start(
            session, org_id=attempt.org_id, project_id=attempt.project_id, attempt_id=attempt.id, inputs=inputs
        )
    except AppError as exc:
        set_status(attempt, "failed", f"verification could not start: {exc.message}")


async def on_verification_done(session: AsyncSession, attempt: RepairAttempt) -> None:
    if attempt.status != "verifying":
        return
    detail = await verification.for_attempt(session, attempt.id)
    if detail is None:
        return
    finding = await session.get(Finding, attempt.finding_id)
    if detail["status"] == "verified":
        set_status(attempt, "awaiting_approval", "verified: every applicable gate passed")
        repo = await github.repository_for_project(session, attempt.project_id)
        main = next((c for c in detail["comparisons"] if c["scope"].startswith("endpoint:")), None)
        await approvals.request(
            session,
            org_id=attempt.org_id,
            subject_type="repair_pr",
            subject_id=attempt.id,
            requested_by=attempt.requested_by,
            summary={
                "finding": finding.title if finding else None,
                "diff_stats": attempt.diff_stats,
                "regression_tests": attempt.regression_test_paths,
                "gates": {g["gate"]: g["status"] for g in detail["gates"] if g["applicable"]},
                "comparison": main,
                "repository": repo.full_name if repo else None,
                "project_id": str(attempt.project_id),
            },
        )
        if finding is not None and finding.status in ("open", "diagnosed", "fixing"):
            finding.status = "fix_proposed"
        return
    failed = [g for g in detail["gates"] if g["applicable"] and g["status"] in ("failed", "errored")]
    reason = "failed gates: " + ", ".join(g["gate"] for g in failed) if failed else "verification failed"
    set_status(attempt, "verification_failed", reason)
    retryable = any(g["status"] == "failed" for g in failed) and attempt.context.get("auto_retry", True)
    h = await session.get(Hypothesis, attempt.hypothesis_id)
    if retryable and attempt.attempt_no < MAX_ATTEMPTS and finding is not None and h is not None:
        feedback = "The previous attempt failed verification:\n" + "\n".join(
            f"- {g['gate']}: {str(g.get('result'))[:800]}" for g in failed
        )
        try:
            await _create(session, finding, h, attempt.requested_by, attempt.constraints, feedback)
        except AppError as exc:
            attempt.status_reason = f"{reason}; no further attempt: {exc.message}"
    elif finding is not None and finding.status == "fixing":
        finding.status = "diagnosed"


async def on_approval(session: AsyncSession, principal: Principal, approval: Approval) -> None:
    attempt = await session.get(RepairAttempt, approval.subject_id, with_for_update=True)
    if attempt is None or attempt.status != "awaiting_approval":
        raise AppError("INVALID_STATE", "this repair is no longer awaiting approval")
    if approval.decision == "approved":
        set_status(attempt, "approved", f"approved by {principal.user_id}")
        await outbox.enqueue(
            session,
            "repairs.open_pr",
            {"attempt_id": str(attempt.id), "org_id": str(attempt.org_id)},
            org_id=attempt.org_id,
        )
    else:
        set_status(attempt, "rejected", approval.reason or "rejected by reviewer")
        finding = await session.get(Finding, attempt.finding_id)
        if finding is not None and finding.status == "fix_proposed":
            finding.status = "diagnosed"


approvals.register_handler("repair_pr", on_approval)


async def open_pr(session: AsyncSession, attempt: RepairAttempt) -> None:
    """Deterministic PR creation after approval (exactly once per attempt)."""
    if attempt.status not in ("approved", "pr_opened"):
        return
    repo = await github.repository_for_project(session, attempt.project_id)
    if repo is None:
        attempt.status_reason = "approved — no GitHub repository is connected; download the patch to apply it"
        return
    if attempt.base_commit_sha in (ARCHIVE_SHA, "") or not attempt.patch_artifact_id:
        attempt.status_reason = "approved — verified against an uploaded archive; download the patch to apply it"
        return
    finding = await session.get(Finding, attempt.finding_id)
    h = await session.get(Hypothesis, attempt.hypothesis_id)
    detail = await verification.for_attempt(session, attempt.id)
    v_inputs = await _verification_inputs(session, attempt.id)
    assert finding is not None and h is not None and detail is not None
    files = {p: v[1] for p, v in {**v_inputs.get("changes", {}), **v_inputs.get("test_changes", {})}.items()}
    body = github_pr.render_body(
        project_id=attempt.project_id,
        finding=_finding_dict(finding),
        hypothesis=_hypothesis_dict(h),
        evidence=await _evidence(session, h.id),
        gates=detail["gates"],
        comparisons=detail["comparisons"],
        test_paths=attempt.regression_test_paths or [],
        explanation=attempt.agent_summary or "",
        diff_stats=attempt.diff_stats,
    )
    pr = await github_pr.reserve(
        session,
        org_id=attempt.org_id,
        repair_attempt_id=attempt.id,
        repository=repo,
        branch=github_pr.branch_name(finding.id, attempt.id),
    )
    pr = await github_pr.open_pull_request(
        session,
        pr,
        repo,
        base_sha=attempt.base_commit_sha,
        files=files,
        pr_title=github_pr.title(finding.title),
        body=body,
    )
    if pr.state == "open":
        set_status(attempt, "pr_opened", pr.url)
    else:
        attempt.status_reason = f"approved — opening the pull request failed: {pr.error}"


async def _verification_inputs(session: AsyncSession, attempt_id: uuid.UUID) -> dict[str, Any]:
    from reliability.modules.verification.models import Verification

    v = (
        await session.execute(select(Verification).where(Verification.repair_attempt_id == attempt_id))
    ).scalar_one_or_none()
    return dict(v.inputs) if v else {}


async def cancel(session: AsyncSession, principal: Principal, attempt: RepairAttempt) -> RepairAttempt:
    require(principal, attempt.org_id, "engineer", project_id=attempt.project_id)
    if attempt.status not in OPEN:
        raise AppError("INVALID_STATE", f"repair is {attempt.status}")
    set_status(attempt, "abandoned", f"cancelled by {principal.user_id}")
    pending = await approvals.for_subject(session, "repair_pr", attempt.id)
    if pending is not None and pending.decision is None:
        pending.decision, pending.reason, pending.decided_at = "rejected", "repair cancelled", datetime.now(UTC)
    return attempt
