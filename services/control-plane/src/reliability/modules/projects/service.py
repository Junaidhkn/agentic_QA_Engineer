"""Projects (one application under test per project)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.projects.models import Project
from reliability.modules.quotas import service as quotas
from reliability.modules.tenancy.policy import require
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7


async def load(
    session: AsyncSession, principal: Principal, project_id: uuid.UUID, minimum: str, scope: str | None = None
) -> Project:
    """Fetch a project the caller may act on with ``minimum`` role, or raise NOT_FOUND/FORBIDDEN."""
    project = await session.get(Project, project_id)
    if project is None or project.archived_at is not None:
        raise AppError("NOT_FOUND", "project not found")
    require(principal, project.org_id, minimum, scope=scope, project_id=project_id)
    return project


async def create(
    session: AsyncSession, principal: Principal, org_id: uuid.UUID, name: str, default_slo: dict[str, Any] | None
) -> Project:
    require(principal, org_id, "admin")
    plan = await quotas.get_plan(session, org_id)
    count = await session.scalar(
        select(func.count()).select_from(Project).where(Project.org_id == org_id, Project.archived_at.is_(None))
    )
    if (count or 0) >= plan.max_projects:
        raise AppError(
            "PLAN_LIMIT_EXCEEDED", f"plan allows {plan.max_projects} project(s)", {"limit": plan.max_projects}
        )
    project = Project(
        id=uuid7(), org_id=org_id, name=name, default_slo=default_slo or {"p95_ms": 500, "error_rate": 0.01}
    )
    session.add(project)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise AppError("CONFLICT", "a project with this name already exists") from exc
    await audit.record(
        session,
        principal.actor,
        "project.created",
        org_id=org_id,
        subject_type="project",
        subject_id=project.id,
        metadata={"name": name},
    )
    return project


async def list_for(session: AsyncSession, principal: Principal, org_id: uuid.UUID | None) -> list[Project]:
    stmt = select(Project).where(Project.archived_at.is_(None)).order_by(Project.created_at.desc())
    if org_id:
        stmt = stmt.where(Project.org_id == org_id)
    if principal.project_ids is not None:
        stmt = stmt.where(Project.id.in_(principal.project_ids))
    return list((await session.execute(stmt)).scalars())
