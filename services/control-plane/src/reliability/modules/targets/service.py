"""Targets: registration, proof of control, re-verification and environment policy (FR-3..5, ADR-0010)."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import dns.asyncresolver
import dns.exception
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.targets import safety
from reliability.modules.targets.models import Target
from reliability.modules.targets.safety import Resolver, check_host_is_public, normalize_url
from reliability.platform import outbox
from reliability.platform.actor import SYSTEM
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7

log = logging.getLogger(__name__)

VERIFICATION_VALIDITY = timedelta(hours=24)
REVERIFY_WINDOW = timedelta(hours=2)
WELL_KNOWN_PATH = "/.well-known/reliability-engineer.txt"
TXT_PREFIX = "_reliability-engineer."
ENVIRONMENTS = ("sandbox", "preview", "staging", "production")

# Load profiles allowed per environment (FR-5). Production: smoke only, tightly capped.
ENVIRONMENT_POLICY: dict[str, dict[str, Any]] = {
    "sandbox": {"profiles": {"smoke", "baseline", "load", "stress", "spike", "soak"}},
    "preview": {"profiles": {"smoke", "baseline", "load", "stress", "spike", "soak"}},
    "staging": {"profiles": {"smoke", "baseline", "load", "stress", "spike", "soak"}},
    "production": {"profiles": {"smoke"}, "max_vus": 5, "max_duration_s": 60, "requires_approval": True},
}

DnsChecker = Callable[[str], Awaitable[list[str]]]
HttpChecker = Callable[[Target], Awaitable[str | None]]


def _dns() -> DnsChecker:
    return globals()["dns_txt_records"]  # looked up at call time so tests can patch it


def _http() -> HttpChecker:
    return globals()["http_well_known"]


def expected_value(target: Target) -> str:
    return f"re-verify={target.verification_token}"


def instructions(target: Target) -> dict[str, Any]:
    value = expected_value(target)
    origin = target.base_url.split("/", 3)[:3]
    return {
        "dns_txt": {"name": f"{TXT_PREFIX}{target.host}", "value": value},
        "http_file": {"url": "/".join(origin) + WELL_KNOWN_PATH, "content": value},
    }


async def dns_txt_records(name: str) -> list[str]:
    try:
        answer = await dns.asyncresolver.resolve(name, "TXT", lifetime=5.0)
    except (dns.exception.DNSException, OSError):
        return []
    return [b"".join(r.strings).decode(errors="replace") for r in answer]


async def http_well_known(target: Target, resolver: Resolver | None = None) -> str | None:
    origin = "/".join(target.base_url.split("/", 3)[:3])
    url = origin + WELL_KNOWN_PATH
    check_host_is_public(target.host, resolver or safety.system_resolver)
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        for _ in range(3):
            try:
                resp = await client.get(url, headers={"User-Agent": "ReliabilityEngineer-Verifier/1.0"})
            except httpx.HTTPError:
                return None
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                nxt = httpx.URL(url).join(location)
                if nxt.host != target.host:  # never follow redirects to other hosts
                    return None
                url = str(nxt)
                continue
            if resp.status_code != 200:
                return None
            return resp.content[:4096].decode(errors="replace")
    return None


async def register(
    session: AsyncSession,
    principal: Principal,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    base_url: str,
    environment: str,
    resolver: Resolver | None = None,
) -> Target:
    if environment not in ENVIRONMENTS:
        raise AppError("VALIDATION_FAILED", "invalid environment", {"allowed": ENVIRONMENTS})
    url = normalize_url(base_url)
    addresses = check_host_is_public(url.host, resolver or safety.system_resolver)
    target = Target(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        base_url=url.base_url,
        host=url.host,
        environment=environment,
        verification_token=secrets.token_urlsafe(24),
        status="pending",
        resolved_ips=addresses,
        created_by=principal.user_id,
    )
    session.add(target)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise AppError("CONFLICT", "this host is already registered for the project") from exc
    await audit.record(
        session,
        principal.actor,
        "target.registered",
        org_id=org_id,
        subject_type="target",
        subject_id=target.id,
        metadata={"host": url.host, "environment": environment},
    )
    return target


async def _check(target: Target, dns_check: DnsChecker, http_check: HttpChecker) -> tuple[str | None, dict[str, Any]]:
    value = expected_value(target)
    records = await dns_check(f"{TXT_PREFIX}{target.host}")
    if any(value in r for r in records):
        return "dns_txt", {"dns_txt": "match"}
    try:
        body = await http_check(target)
    except AppError as exc:
        return None, {"dns_txt": "no match", "http_file": exc.code}
    if body is not None and value in body:
        return "http_file", {"dns_txt": "no match", "http_file": "match"}
    return None, {
        "dns_txt": "no match" if not records else f"{len(records)} record(s), none match",
        "http_file": "not found" if body is None else "content does not match",
    }


async def verify(
    session: AsyncSession,
    actor_principal: Principal | None,
    target: Target,
    dns_check: DnsChecker | None = None,
    http_check: HttpChecker | None = None,
) -> Target:
    method, detail = await _check(target, dns_check or _dns(), http_check or _http())
    now = datetime.now(UTC)
    target.last_checked_at, target.last_check_detail = now, detail
    actor = actor_principal.actor if actor_principal else SYSTEM
    if method:
        target.status, target.verification_method = "verified", method
        target.verified_at, target.verification_expires_at = now, now + VERIFICATION_VALIDITY
        await audit.record(
            session,
            actor,
            "target.verified",
            org_id=target.org_id,
            subject_type="target",
            subject_id=target.id,
            metadata={"method": method, "host": target.host},
        )
        await session.flush()
        return target
    previously_verified = target.status == "verified"
    target.status = "failed"
    await audit.record(
        session,
        actor,
        "target.verification_failed",
        org_id=target.org_id,
        subject_type="target",
        subject_id=target.id,
        metadata={"host": target.host, **detail},
    )
    if previously_verified:  # FR-4: abort in-flight runs
        await outbox.enqueue(
            session,
            "targets.revoked",
            {"target_id": str(target.id), "org_id": str(target.org_id)},
            org_id=target.org_id,
        )
    await session.flush()
    return target


async def revoke(session: AsyncSession, principal: Principal, target: Target) -> None:
    target.status = "revoked"
    target.verification_expires_at = None
    await outbox.enqueue(
        session, "targets.revoked", {"target_id": str(target.id), "org_id": str(target.org_id)}, org_id=target.org_id
    )
    await audit.record(
        session,
        principal.actor,
        "target.revoked",
        org_id=target.org_id,
        subject_type="target",
        subject_id=target.id,
        metadata={"host": target.host},
    )


async def get(session: AsyncSession, target_id: uuid.UUID) -> Target:
    target = await session.get(Target, target_id)
    if target is None:
        raise AppError("NOT_FOUND", "target not found")
    return target


async def list_for_project(session: AsyncSession, project_id: uuid.UUID) -> list[Target]:
    return list(
        (
            await session.execute(select(Target).where(Target.project_id == project_id).order_by(Target.created_at))
        ).scalars()
    )


def policy_violation(target: Target, profile_type: str, max_vus: int, duration_s: int) -> str | None:
    """Return a reason if the environment policy forbids this profile, else None (FR-5)."""
    policy = ENVIRONMENT_POLICY[target.environment]
    if profile_type not in policy["profiles"]:
        return f"{profile_type} tests are not allowed against {target.environment} targets"
    if "max_vus" in policy and max_vus > policy["max_vus"]:
        return f"at most {policy['max_vus']} VUs against {target.environment} targets"
    if "max_duration_s" in policy and duration_s > policy["max_duration_s"]:
        return f"at most {policy['max_duration_s']} s against {target.environment} targets"
    return None


async def reverify_due(dns_check: DnsChecker | None = None, http_check: HttpChecker | None = None) -> int:
    """Hourly sweep: re-check targets whose verification expires within the window."""
    async with system_tx() as session:
        due = list(
            (
                await session.execute(
                    select(Target)
                    .where(
                        Target.status == "verified",
                        Target.verification_expires_at < datetime.now(UTC) + REVERIFY_WINDOW,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(200)
                )
            ).scalars()
        )
        for target in due:
            await verify(session, None, target, dns_check, http_check)
    return len(due)
