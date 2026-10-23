"""Organizations, memberships and invitations (FR-1, FR-30)."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.models import User
from reliability.modules.identity.service import Principal
from reliability.modules.notifications.service import get_email_sender
from reliability.modules.tenancy.models import Invitation, Membership, Organization
from reliability.platform.crypto import hmac_digest
from reliability.platform.db import set_tenant, system_tx, tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.settings import get_settings

INVITE_TTL = timedelta(days=7)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "org"
    if len(base) < 3:
        base = f"{base}-org"
    return f"{base}-{secrets.token_hex(3)}"


async def _insert_org(session: AsyncSession, principal: Principal, org_id: uuid.UUID, name: str) -> Organization:
    assert principal.user_id is not None
    await set_tenant(session, principal.user_id, [*principal.org_ids, org_id])
    org = Organization(id=org_id, name=name, slug=_slug(name), plan_id="free")
    session.add(org)
    await session.flush()
    session.add(Membership(org_id=org_id, user_id=principal.user_id, role="owner"))
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "org.created",
        org_id=org_id,
        subject_type="organization",
        subject_id=org_id,
        metadata={"name": name},
    )
    return org


async def create_org_for(principal: Principal, name: str) -> uuid.UUID:
    org_id = uuid7()
    async with tenant_tx(user_id=principal.user_id, org_ids=[org_id]) as session:
        await _insert_org(session, principal, org_id, name)
    return org_id


async def create_org(session: AsyncSession, principal: Principal, name: str) -> Organization:
    if principal.kind != "user":
        raise AppError("FORBIDDEN", "only users can create organizations")
    org = await _insert_org(session, principal, uuid7(), name)
    principal.org_roles[org.id] = "owner"
    return org


async def list_orgs(session: AsyncSession, principal: Principal) -> list[tuple[Organization, str]]:
    orgs = (await session.execute(select(Organization).where(Organization.id.in_(principal.org_ids)))).scalars()
    return [(o, principal.org_roles[o.id]) for o in orgs]


async def get_org(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise AppError("NOT_FOUND", "organization not found")
    return org


async def list_members(session: AsyncSession, org_id: uuid.UUID) -> list[tuple[Membership, User]]:
    rows = await session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id)
        .order_by(Membership.created_at)
    )
    return [(m, u) for m, u in rows.all()]


async def invite(session: AsyncSession, principal: Principal, org_id: uuid.UUID, email: str, role: str) -> Invitation:
    if role not in ("admin", "engineer", "viewer"):
        raise AppError("VALIDATION_FAILED", "role must be admin, engineer or viewer")
    if role == "admin" and principal.org_roles.get(org_id) != "owner":
        raise AppError("FORBIDDEN", "only owners can invite admins")
    token = secrets.token_urlsafe(32)
    inv = Invitation(
        id=uuid7(),
        org_id=org_id,
        email=email.lower(),
        role=role,
        token_hash=hmac_digest(token),
        invited_by=principal.user_id,
        expires_at=datetime.now(UTC) + INVITE_TTL,
    )
    session.add(inv)
    await session.flush()
    org = await get_org(session, org_id)
    link = f"{get_settings().web_base_url}/invitations/{token}"
    get_email_sender().send(
        email, f"You're invited to {org.name} on Reliability Engineer", f"Accept within 7 days: {link}"
    )
    await audit.record(
        session,
        principal.actor,
        "member.invited",
        org_id=org_id,
        subject_type="invitation",
        subject_id=inv.id,
        metadata={"role": role, "email_domain": email.split("@")[-1]},
    )
    return inv


async def accept_invitation(principal: Principal, token: str) -> tuple[uuid.UUID, str]:
    if principal.kind != "user" or principal.user_id is None or principal.email is None:
        raise AppError("FORBIDDEN", "only users can accept invitations")
    async with system_tx() as session:  # the invitee is not yet a member of the org
        inv = (
            await session.execute(
                select(Invitation).where(Invitation.token_hash == hmac_digest(token)).with_for_update()
            )
        ).scalar_one_or_none()
        if inv is None or inv.accepted_at is not None or inv.expires_at < datetime.now(UTC):
            raise AppError("NOT_FOUND", "invitation not found or expired")
        if inv.email.lower() != principal.email.lower():
            raise AppError("FORBIDDEN", "invitation was sent to a different email address")
        existing = await session.get(Membership, (inv.org_id, principal.user_id))
        if existing is None:
            session.add(Membership(org_id=inv.org_id, user_id=principal.user_id, role=inv.role))
        inv.accepted_at = datetime.now(UTC)
        await session.flush()
        await audit.record(
            session,
            principal.actor,
            "member.joined",
            org_id=inv.org_id,
            subject_type="user",
            subject_id=principal.user_id,
            metadata={"role": inv.role},
        )
        return inv.org_id, (existing.role if existing else inv.role)


async def _owner_count(session: AsyncSession, org_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Membership).where(Membership.org_id == org_id, Membership.role == "owner")
        )
        or 0
    )


async def change_role(
    session: AsyncSession, principal: Principal, org_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Membership:
    if role not in ("owner", "admin", "engineer", "viewer"):
        raise AppError("VALIDATION_FAILED", "invalid role")
    m = await session.get(Membership, (org_id, user_id), with_for_update=True)
    if m is None:
        raise AppError("NOT_FOUND", "member not found")
    if m.role == "owner" and role != "owner" and await _owner_count(session, org_id) <= 1:
        raise AppError("CONFLICT", "an organization must keep at least one owner")
    old, m.role = m.role, role
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "member.role_changed",
        org_id=org_id,
        subject_type="user",
        subject_id=user_id,
        metadata={"from": old, "to": role},
    )
    return m


async def remove_member(session: AsyncSession, principal: Principal, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    m = await session.get(Membership, (org_id, user_id), with_for_update=True)
    if m is None:
        raise AppError("NOT_FOUND", "member not found")
    if m.role == "owner" and await _owner_count(session, org_id) <= 1:
        raise AppError("CONFLICT", "an organization must keep at least one owner")
    if m.role == "owner" and principal.org_roles.get(org_id) != "owner":
        raise AppError("FORBIDDEN", "only owners can remove owners")
    await session.delete(m)
    await session.flush()
    await audit.record(
        session, principal.actor, "member.removed", org_id=org_id, subject_type="user", subject_id=user_id
    )
