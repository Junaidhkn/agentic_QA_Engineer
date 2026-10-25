"""Authentication: users (OIDC or dev) and API keys (FR-1, FR-30, FR-32, ADR-0018)."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.identity.models import ApiKey, User
from reliability.modules.identity.verifier import VerifiedIdentity, get_oidc_verifier, verify_dev_token
from reliability.platform.actor import Actor
from reliability.platform.crypto import constant_time_equals, hmac_digest
from reliability.platform.db import system_tx, tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

API_KEY_PREFIX = "rek_"
API_KEY_SCOPES = frozenset({"runs:read", "runs:write", "projects:read"})


@dataclass
class Principal:
    kind: str  # "user" | "api_key"
    actor: Actor
    org_roles: dict[uuid.UUID, str]
    user_id: uuid.UUID | None = None
    email: str | None = None
    mfa: bool = False
    api_key_id: uuid.UUID | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    project_ids: frozenset[uuid.UUID] | None = None

    @property
    def org_ids(self) -> list[uuid.UUID]:
        return list(self.org_roles)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("UNAUTHENTICATED", "missing bearer token")
    token = authorization[7:].strip()
    if not token:
        raise AppError("UNAUTHENTICATED", "missing bearer token")
    return token


async def authenticate(authorization: str | None, actor_base: Actor) -> Principal:
    token = _bearer(authorization)
    if token.startswith(API_KEY_PREFIX):
        return await _authenticate_api_key(token, actor_base)
    identity = verify_dev_token(token)
    if identity is None:
        verifier = get_oidc_verifier()
        if verifier is None:
            raise AppError("UNAUTHENTICATED", "identity provider not configured")
        identity = verifier.verify(token)
    return await _authenticate_user(identity, actor_base)


async def _authenticate_user(identity: VerifiedIdentity, actor_base: Actor) -> Principal:
    async with tenant_tx() as session:
        await session.execute(
            insert(User)
            .values(
                id=uuid7(),
                idp_subject=identity.subject,
                email=identity.email,
                display_name=identity.name,
                mfa_enabled=identity.mfa,
            )
            .on_conflict_do_nothing(index_elements=["idp_subject"])
        )
        user = (await session.execute(select(User).where(User.idp_subject == identity.subject))).scalar_one()
        if user.disabled_at is not None:
            raise AppError("UNAUTHENTICATED", "account disabled")
        if user.email != identity.email or user.mfa_enabled != identity.mfa:
            user.email, user.mfa_enabled = identity.email, identity.mfa
        user.last_seen_at = datetime.now(UTC)
        user_id = user.id

    async with tenant_tx(user_id=user_id) as session:
        rows = (
            await session.execute(text("SELECT org_id, role FROM memberships WHERE user_id = :u"), {"u": user_id})
        ).all()
    org_roles = {r.org_id: r.role for r in rows}
    actor = Actor("user", str(user_id), actor_base.ip, actor_base.user_agent, actor_base.request_id)
    principal = Principal("user", actor, org_roles, user_id=user_id, email=identity.email, mfa=identity.mfa)
    if not org_roles:
        # FR-1: a new user gets an organization with themselves as owner.
        from reliability.modules.tenancy import service as tenancy

        org_id = await tenancy.create_org_for(principal, name=f"{identity.name or identity.email.split('@')[0]}'s org")
        principal.org_roles = {org_id: "owner"}
    return principal


async def _authenticate_api_key(token: str, actor_base: Actor) -> Principal:
    try:
        _, prefix_part, secret = token.split("_", 2)
    except ValueError as exc:
        raise AppError("UNAUTHENTICATED", "malformed API key") from exc
    prefix = f"{API_KEY_PREFIX}{prefix_part}"
    async with system_tx() as session:  # lookup by prefix crosses tenants by design
        key = (await session.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))).scalar_one_or_none()
        now = datetime.now(UTC)
        if (
            key is None
            or not constant_time_equals(key.key_hash, hmac_digest(token))
            or key.revoked_at is not None
            or key.expires_at < now
        ):
            raise AppError("UNAUTHENTICATED", "invalid API key")
        if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=5):
            await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
    del secret
    actor = Actor("api_key", str(key.id), actor_base.ip, actor_base.user_agent, actor_base.request_id)
    return Principal(
        "api_key",
        actor,
        {key.org_id: "engineer"},
        api_key_id=key.id,
        scopes=frozenset(key.scopes),
        project_ids=frozenset(key.project_ids) if key.project_ids else None,
    )


@dataclass(frozen=True)
class CreatedKey:
    key: ApiKey
    secret: str


async def create_api_key(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    scopes: list[str],
    project_ids: list[uuid.UUID] | None,
    expires_in_days: int,
) -> CreatedKey:
    unknown = set(scopes) - API_KEY_SCOPES
    if unknown or not scopes:
        raise AppError("VALIDATION_FAILED", "invalid scopes", {"allowed": sorted(API_KEY_SCOPES)})
    if not 1 <= expires_in_days <= 365:
        raise AppError("VALIDATION_FAILED", "expires_in_days must be between 1 and 365")
    prefix = f"{API_KEY_PREFIX}{secrets.token_hex(4)}"
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    key = ApiKey(
        id=uuid7(),
        org_id=org_id,
        name=name,
        key_prefix=prefix,
        key_hash=hmac_digest(token),
        scopes=scopes,
        project_ids=project_ids,
        created_by=created_by,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    session.add(key)
    await session.flush()
    return CreatedKey(key, token)


async def revoke_api_key(session: AsyncSession, org_id: uuid.UUID, key_id: uuid.UUID) -> bool:
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.org_id == org_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def list_api_keys(session: AsyncSession, org_id: uuid.UUID) -> list[ApiKey]:
    return list(
        (
            await session.execute(select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc()))
        ).scalars()
    )


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
