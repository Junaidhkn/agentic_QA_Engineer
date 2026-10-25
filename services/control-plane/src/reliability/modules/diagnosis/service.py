"""Diagnosis lifecycle and read model (FR-21, FR-22).

Only hypotheses with at least one verified supporting citation are ever stored, and the read
path filters on the same condition (ADR-0015 confirmation), so an unverified explanation can
never be displayed as a root cause.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.diagnosis.models import Diagnosis, Hypothesis, HypothesisEvidence
from reliability.modules.discovery.models import AgentThread
from reliability.modules.findings.models import Evidence, Finding
from reliability.modules.identity.service import Principal
from reliability.platform import outbox
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

ACTIVE = ("queued", "collecting", "hypothesizing", "checking", "experimenting")
DISCLAIMER = (
    "Hypotheses are ranked explanations backed by the evidence shown; they are not guaranteed root causes. "
    "Confidence is computed from the evidence, not from the model."
)


async def start(session: AsyncSession, principal: Principal | None, finding: Finding) -> Diagnosis:
    running = (
        await session.execute(
            select(Diagnosis.id).where(Diagnosis.finding_id == finding.id, Diagnosis.status.in_(ACTIVE))
        )
    ).first()
    if running:
        raise AppError("CONFLICT", "a diagnosis is already running for this finding", {"diagnosis_id": str(running[0])})
    diag_id = uuid7()
    diag = Diagnosis(
        id=diag_id,
        org_id=finding.org_id,
        finding_id=finding.id,
        status="queued",
        agent_thread_id=f"diagnosis:{diag_id}",
        requested_by=principal.user_id if principal else None,
    )
    session.add(diag)
    session.add(
        AgentThread(thread_id=diag.agent_thread_id, org_id=finding.org_id, graph="diagnosis", subject_id=diag_id)
    )
    await session.flush()
    if finding.status == "open":
        finding.status = "diagnosing"
    await outbox.enqueue(
        session, "diagnosis.run", {"diagnosis_id": diag_id, "org_id": finding.org_id}, org_id=finding.org_id
    )
    if principal is not None:
        await audit.record(
            session,
            principal.actor,
            "diagnosis.started",
            org_id=finding.org_id,
            subject_type="finding",
            subject_id=finding.id,
            metadata={"diagnosis_id": str(diag_id)},
        )
    return diag


async def get(session: AsyncSession, diagnosis_id: uuid.UUID) -> Diagnosis:
    diag = await session.get(Diagnosis, diagnosis_id)
    if diag is None:
        raise AppError("NOT_FOUND", "diagnosis not found")
    return diag


async def for_finding(session: AsyncSession, finding_id: uuid.UUID) -> list[Diagnosis]:
    rows = await session.execute(
        select(Diagnosis).where(Diagnosis.finding_id == finding_id).order_by(Diagnosis.created_at.desc()).limit(20)
    )
    return list(rows.scalars())


async def hypotheses(session: AsyncSession, diagnosis_id: uuid.UUID) -> list[dict[str, Any]]:
    verified = exists().where(
        HypothesisEvidence.hypothesis_id == Hypothesis.id,
        HypothesisEvidence.relation == "supports",
        HypothesisEvidence.check_result == "verified",
    )
    rows = list(
        (
            await session.execute(
                select(Hypothesis).where(Hypothesis.diagnosis_id == diagnosis_id, verified).order_by(Hypothesis.rank)
            )
        ).scalars()
    )
    out = []
    for h in rows:
        cites = (
            await session.execute(
                select(HypothesisEvidence, Evidence)
                .join(Evidence, Evidence.id == HypothesisEvidence.evidence_id)
                .where(HypothesisEvidence.hypothesis_id == h.id)
            )
        ).all()
        out.append(
            {
                "id": h.id,
                "rank": h.rank,
                "category": h.category,
                "statement": h.statement,
                "mechanism": h.mechanism,
                "fix_direction": h.fix_direction,
                "code_locations": h.code_locations,
                "confidence": h.confidence,
                "confidence_factors": h.confidence_factors,
                "status": h.status,
                "user_verdict": h.user_verdict,
                "user_verdict_note": h.user_verdict_note,
                "evidence": [
                    {
                        "id": e.id,
                        "kind": e.kind,
                        "summary": e.summary,
                        "locator": e.locator,
                        "relation": he.relation,
                        "claim": he.claim,
                        "check": he.check_result,
                    }
                    for he, e in cites
                ],
            }
        )
    return out


async def feedback(
    session: AsyncSession, principal: Principal, hypothesis_id: uuid.UUID, verdict: str, note: str | None
) -> Hypothesis:
    h = await session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise AppError("NOT_FOUND", "hypothesis not found")
    h.user_verdict, h.user_verdict_by, h.user_verdict_note = verdict, principal.user_id, note
    await audit.record(
        session,
        principal.actor,
        "hypothesis.feedback",
        org_id=h.org_id,
        subject_type="hypothesis",
        subject_id=h.id,
        metadata={"verdict": verdict},
    )
    return h


async def precision(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Online precision from user feedback (evaluation §5): confirmed ÷ (confirmed + refuted)."""
    rows = (
        await session.execute(
            select(Hypothesis.rank, Hypothesis.confidence, Hypothesis.user_verdict).where(
                Hypothesis.org_id == org_id, Hypothesis.user_verdict.is_not(None)
            )
        )
    ).all()
    judged = [r for r in rows if r.user_verdict in ("confirmed", "refuted")]
    by_band: dict[str, dict[str, int]] = {}
    for r in judged:
        b = by_band.setdefault(r.confidence, {"confirmed": 0, "refuted": 0})
        b[r.user_verdict] += 1
    confirmed = sum(1 for r in judged if r.user_verdict == "confirmed")
    return {
        "judged": len(judged),
        "confirmed": confirmed,
        "precision": round(confirmed / len(judged), 3) if judged else None,
        "by_confidence": by_band,
    }


async def save_result(session: AsyncSession, diag: Diagnosis, result: dict[str, Any]) -> None:
    for rank, h in enumerate(result.get("hypotheses", []), 1):
        cites: dict[str, dict[str, Any]] = {}
        for c in h.get("citations", []):
            cites.setdefault(c["evidence_id"], c)
        valid_ids = {
            str(i)
            for i in (
                await session.execute(
                    select(Evidence.id).where(
                        Evidence.id.in_([uuid.UUID(i) for i in cites if _is_uuid(i)]), Evidence.org_id == diag.org_id
                    )
                )
            ).scalars()
        }
        if not any(
            c["relation"] == "supports" and c["check_result"] == "verified" and eid in valid_ids
            for eid, c in cites.items()
        ):
            continue
        hyp = Hypothesis(
            id=uuid7(),
            org_id=diag.org_id,
            diagnosis_id=diag.id,
            rank=rank,
            category=h["category"],
            statement=h["statement"][:2000],
            mechanism=h["mechanism"][:4000],
            fix_direction=(h.get("fix_direction") or "")[:2000] or None,
            code_locations=h.get("code_locations", []),
            confidence=h["confidence"],
            confidence_factors={**h["confidence_factors"], "origin": h.get("origin")},
            status=h["status"],
        )
        session.add(hyp)
        await session.flush()
        for eid, c in cites.items():
            if eid in valid_ids:
                session.add(
                    HypothesisEvidence(
                        hypothesis_id=hyp.id,
                        evidence_id=uuid.UUID(eid),
                        org_id=diag.org_id,
                        relation=c["relation"],
                        claim=c["claim"][:1000],
                        check_result=c["check_result"],
                    )
                )
    diag.status = result.get("status", "completed")
    diag.next_steps = result.get("next_steps") or []
    info = dict(result.get("model_info") or {})
    diag.token_usage = {"total": info.pop("tokens", 0)}
    diag.model_info = info
    diag.finished_at = datetime.now(UTC)
    finding = await session.get(Finding, diag.finding_id)
    if finding is not None and finding.status == "diagnosing":
        finding.status = "diagnosed" if diag.status == "completed" else "open"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
