"""Platform invariants (evaluation §5), checked hourly on the live database and after every
restore drill. Each invariant is a count of violating rows; any non-zero count pages.

* every stored hypothesis has ≥ 1 verified supporting citation (ADR-0015);
* a repair is verified/approved/PR'd only with every applicable gate passed (ADR-0016);
* every pull request has an approved approval (ADR-0017);
* tenant-owned rows reference projects of the same organization (RLS is keyed on ``org_id``);
* every audit chain verifies and matches its daily anchors (ADR-0023).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit.service import verify_chain

VERIFIED_STATES = "('verified','awaiting_approval','approved','pr_opened')"
PROJECT_TABLES = ("runs", "findings", "workflows", "targets", "repair_attempts", "reports", "test_suites")

INVARIANTS: dict[str, str] = {
    "hypothesis_without_verified_support": """
        SELECT count(*) FROM hypotheses h WHERE NOT EXISTS (
          SELECT 1 FROM hypothesis_evidence e
          WHERE e.hypothesis_id = h.id AND e.relation = 'supports' AND e.check_result = 'verified')
    """,
    "verified_repair_without_passed_gates": f"""
        SELECT count(*) FROM repair_attempts ra
        WHERE ra.status IN {VERIFIED_STATES} AND (
          NOT EXISTS (SELECT 1 FROM verification_gates g WHERE g.repair_attempt_id = ra.id AND g.applicable)
          OR EXISTS (SELECT 1 FROM verification_gates g
                     WHERE g.repair_attempt_id = ra.id AND g.applicable AND g.status <> 'passed'))
    """,  # noqa: S608 - interpolates a constant
    "pull_request_without_approval": """
        SELECT count(*) FROM pull_requests p WHERE NOT EXISTS (
          SELECT 1 FROM approvals a WHERE a.decision = 'approved' AND (
            (a.subject_type = 'repair_pr' AND a.subject_id = p.repair_attempt_id) OR
            (a.subject_type = 'suite_pr' AND a.subject_id = p.suite_id)))
    """,
    **{
        f"cross_tenant_{table}": f"""
            SELECT count(*) FROM {table} t JOIN projects p ON p.id = t.project_id WHERE t.org_id <> p.org_id
        """  # noqa: S608 - table names are constants
        for table in PROJECT_TABLES
    },
}

ANCHOR_MISMATCHES = """
    SELECT a.org_key, a.anchor_date FROM audit_anchors a
    JOIN audit_events e ON e.seq = a.last_seq AND coalesce(e.org_id::text, 'platform') = a.org_key
    WHERE e.hash <> a.last_hash
"""


async def run_checks(session: AsyncSession) -> dict[str, Any]:
    """Counts per invariant plus audit-chain results; ``ok`` only when everything is zero."""
    counts = {name: int(await session.scalar(text(sql)) or 0) for name, sql in INVARIANTS.items()}
    chains = [r[0] for r in (await session.execute(text("SELECT DISTINCT org_id FROM audit_events"))).all()]
    broken: list[dict[str, Any]] = []
    checked = 0
    for org_id in chains:
        result = await verify_chain(session, org_id)
        checked += result.checked
        if not result.ok:
            broken.append({"chain": str(org_id) if org_id else "platform", "broken_at_seq": result.broken_at_seq})
    for row in (await session.execute(text(ANCHOR_MISMATCHES))).all():
        broken.append({"chain": row.org_key, "anchor_date": row.anchor_date.isoformat(), "anchor": "mismatch"})
    return {
        "ok": not any(counts.values()) and not broken,
        "invariants": counts,
        "broken_chains": broken,
        "audit_events_checked": checked,
        "chains": len(chains),
    }
