"""Candidate and approved workflows (FR-9, FR-13, ADR-0013)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.projects.models import Project
from reliability.modules.tenancy.policy import role_at_least
from reliability.modules.workflows.ir import validate_ir
from reliability.modules.workflows.models import Workflow
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7


def _destructive(ir: dict[str, Any]) -> bool:
    return bool(ir.get("destructive")) or any(
        (s.get("request") or {}).get("method") == "DELETE"
        for s in [*ir.get("steps", []), *ir.get("api_equivalent", [])]
    )


async def create_manual(session: AsyncSession, principal: Principal, project: Project, ir: dict[str, Any]) -> Workflow:
    validate_ir(ir)
    destructive = _destructive(ir)
    if destructive and not role_at_least(principal.org_roles.get(project.org_id), "admin"):
        raise AppError("FORBIDDEN", "destructive workflows must be created by an admin")
    existing = (
        await session.execute(select(Workflow).where(Workflow.project_id == project.id, Workflow.key == ir["key"]))
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError("CONFLICT", f"workflow key {ir['key']} already exists")
    now = datetime.now(UTC)
    wf = Workflow(
        id=uuid7(),
        org_id=project.org_id,
        project_id=project.id,
        key=ir["key"],
        title=ir["title"],
        kind=ir["kind"],
        criticality=ir["criticality"],
        source="manual",
        status="approved",
        spec=ir,
        destructive=destructive,
        approved_by=principal.user_id,
        approved_at=now,
    )
    session.add(wf)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "workflow.created",
        org_id=project.org_id,
        subject_type="workflow",
        subject_id=wf.id,
        metadata={"key": wf.key, "destructive": destructive},
    )
    return wf


async def upsert_candidate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    ir: dict[str, Any],
    source: str,
    confidence: float,
    discovery_run_id: uuid.UUID | None,
) -> Workflow | None:
    """Insert or refresh a discovered candidate. Never overwrites approved/rejected/manual workflows."""
    validate_ir(ir)
    existing = (
        await session.execute(select(Workflow).where(Workflow.project_id == project_id, Workflow.key == ir["key"]))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == "candidate" and existing.source != "manual":
            existing.spec, existing.title = ir, ir["title"]
            existing.discovery_confidence = Decimal(str(round(confidence, 2)))
            existing.discovery_run_id = discovery_run_id
            existing.spec_version += 1
            return existing
        return None
    wf = Workflow(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        key=ir["key"],
        title=ir["title"],
        kind=ir["kind"],
        criticality=ir["criticality"],
        source=source,
        status="candidate",
        spec=ir,
        destructive=_destructive(ir),
        discovery_confidence=Decimal(str(round(confidence, 2))),
        discovery_run_id=discovery_run_id,
    )
    session.add(wf)
    await session.flush()
    return wf


async def get(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise AppError("NOT_FOUND", "workflow not found")
    return wf


async def list_for(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
    kind: str | None = None,
    source: str | None = None,
) -> list[Workflow]:
    stmt = select(Workflow).where(Workflow.project_id == project_id)
    for col, value in ((Workflow.status, status), (Workflow.kind, kind), (Workflow.source, source)):
        if value:
            stmt = stmt.where(col == value)
    order = {"critical": 0, "high": 1, "normal": 2}
    rows = list((await session.execute(stmt.order_by(Workflow.key))).scalars())
    return sorted(rows, key=lambda w: (order[w.criticality], w.key))


async def edit(
    session: AsyncSession,
    principal: Principal,
    wf: Workflow,
    *,
    spec: dict[str, Any] | None,
    criticality: str | None,
    status: str | None,
) -> Workflow:
    if spec is not None:
        if spec.get("key") != wf.key:
            raise AppError("VALIDATION_FAILED", "workflow key cannot change")
        validate_ir(spec)
        wf.spec, wf.title, wf.kind = spec, spec["title"], spec["kind"]
        wf.destructive = _destructive(spec)
        wf.spec_version += 1
    if criticality is not None:
        wf.criticality = criticality
        wf.spec = {**wf.spec, "criticality": criticality}
    if status is not None:
        await _set_status(principal, wf, status)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "workflow.updated",
        org_id=wf.org_id,
        subject_type="workflow",
        subject_id=wf.id,
        metadata={"status": wf.status, "spec_version": wf.spec_version},
    )
    return wf


async def _set_status(principal: Principal, wf: Workflow, status: str) -> None:
    if status not in ("approved", "rejected", "retired", "candidate"):
        raise AppError("VALIDATION_FAILED", "invalid status")
    if status == "approved" and wf.destructive and not role_at_least(principal.org_roles.get(wf.org_id), "admin"):
        raise AppError("FORBIDDEN", "approving a destructive workflow requires the admin role")
    wf.status = status
    if status == "approved":
        wf.approved_by, wf.approved_at = principal.user_id, datetime.now(UTC)


async def bulk_review(
    session: AsyncSession,
    principal: Principal,
    project_id: uuid.UUID,
    approve: list[uuid.UUID],
    reject: list[uuid.UUID],
) -> int:
    changed = 0
    for ids, status in ((approve, "approved"), (reject, "rejected")):
        if not ids:
            continue
        rows = (
            await session.execute(select(Workflow).where(Workflow.project_id == project_id, Workflow.id.in_(ids)))
        ).scalars()
        for wf in rows:
            await _set_status(principal, wf, status)
            changed += 1
    await session.flush()
    if changed:
        project_org = await session.get(Project, project_id)
        await audit.record(
            session,
            principal.actor,
            "workflows.reviewed",
            org_id=project_org.org_id if project_org else None,
            subject_type="project",
            subject_id=project_id,
            metadata={"approved": len(approve), "rejected": len(reject)},
        )
    return changed


async def status_counts(session: AsyncSession, project_id: uuid.UUID) -> dict[str, dict[str, int]]:
    rows = (
        await session.execute(
            select(Workflow.source, Workflow.status, func.count())
            .where(Workflow.project_id == project_id)
            .group_by(Workflow.source, Workflow.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for source, status, n in rows:
        out.setdefault(source, {})[status] = n
    return out
