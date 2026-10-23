"""GitHub App installation, repository linking, scoped tokens and snapshots (FR-2, ADR-0017)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.audit import service as audit
from reliability.modules.identity.service import Principal
from reliability.modules.integrations.github.client import GitHubClient, get_client
from reliability.modules.integrations.github.models import GithubInstallation, PullRequest, Repository
from reliability.platform.actor import SYSTEM, Actor
from reliability.platform.crypto import sign_token, verify_token
from reliability.platform.db import system_tx
from reliability.platform.errors import AppError
from reliability.platform.ids import uuid7
from reliability.platform.redis_client import get_redis
from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)
STATE_AUDIENCE = "github-install"
READ_PERMISSIONS = {"contents": "read", "metadata": "read"}
PR_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}


def install_url(principal: Principal, org_id: uuid.UUID) -> str:
    s = get_settings()
    if not s.github_app_slug:
        raise AppError("NOT_CONFIGURED", "GitHub App is not configured (RE_GITHUB_APP_SLUG)")
    state = sign_token({"org": str(org_id), "user": str(principal.user_id)}, STATE_AUDIENCE, 900)
    return f"https://github.com/apps/{s.github_app_slug}/installations/new?state={state}"


async def _user_can_access(
    installation_id: int, code: str | None, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    s = get_settings()
    if not (s.github_client_id and s.github_client_secret):
        return s.env in ("local", "test")  # cannot prove access without OAuth; only allowed locally
    if not code:
        return False
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as c:
        resp = await c.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": s.github_client_id,
                "code": code,
                "client_secret": s.github_client_secret.get_secret_value(),
            },
        )
    token = resp.json().get("access_token") if resp.status_code == 200 else None
    if not token:
        return False
    return installation_id in await GitHubClient.user_installations(token, s.github_api_url, transport)


async def complete_installation(
    installation_id: int, state: str, code: str | None, transport: httpx.AsyncBaseTransport | None = None
) -> uuid.UUID:
    claims = verify_token(state, STATE_AUDIENCE)
    org_id, user_id = uuid.UUID(claims["org"]), claims["user"]
    if not await _user_can_access(installation_id, code, transport):
        raise AppError("FORBIDDEN", "could not confirm you have access to this GitHub installation")
    info = await get_client().get_installation(installation_id)
    async with system_tx() as session:
        existing = (
            await session.execute(
                select(GithubInstallation).where(GithubInstallation.installation_id == installation_id)
            )
        ).scalar_one_or_none()
        if existing and existing.org_id != org_id:
            raise AppError("CONFLICT", "this GitHub installation is linked to another organization")
        if existing is None:
            session.add(
                GithubInstallation(
                    id=uuid7(), org_id=org_id, installation_id=installation_id, account_login=info["account"]["login"]
                )
            )
        await audit.record(
            session,
            Actor("user", user_id),
            "github.installed",
            org_id=org_id,
            subject_type="github_installation",
            subject_id=str(installation_id),
            metadata={"account": info["account"]["login"]},
        )
    return org_id


async def installations(session: AsyncSession, org_id: uuid.UUID) -> list[GithubInstallation]:
    return list(
        (
            await session.execute(
                select(GithubInstallation).where(
                    GithubInstallation.org_id == org_id, GithubInstallation.suspended_at.is_(None)
                )
            )
        ).scalars()
    )


async def list_repositories(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    client = get_client()
    out: list[dict[str, Any]] = []
    for inst in await installations(session, org_id):
        token = await client.installation_token(inst.installation_id, permissions={"metadata": "read"})
        for repo in await client.list_repositories(token.token):
            out.append(
                {
                    "installation_id": str(inst.id),
                    "github_repo_id": repo["id"],
                    "full_name": repo["full_name"],
                    "default_branch": repo["default_branch"],
                    "private": repo["private"],
                }
            )
    return out


async def link_repository(
    session: AsyncSession,
    principal: Principal,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    installation_uuid: uuid.UUID,
    github_repo_id: int,
) -> Repository:
    inst = await session.get(GithubInstallation, installation_uuid)
    if inst is None or inst.org_id != org_id:
        raise AppError("NOT_FOUND", "installation not found")
    client = get_client()
    token = await client.installation_token(inst.installation_id, permissions={"metadata": "read"})
    repo = next((r for r in await client.list_repositories(token.token) if r["id"] == github_repo_id), None)
    if repo is None:
        raise AppError("NOT_FOUND", "repository is not accessible to the installation")
    existing = (
        await session.execute(select(Repository).where(Repository.project_id == project_id))
    ).scalar_one_or_none()
    if existing:
        raise AppError("CONFLICT", "project already has a linked repository")
    row = Repository(
        id=uuid7(),
        org_id=org_id,
        project_id=project_id,
        installation_id=inst.id,
        github_repo_id=github_repo_id,
        full_name=repo["full_name"],
        default_branch=repo["default_branch"],
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        principal.actor,
        "repository.linked",
        org_id=org_id,
        subject_type="repository",
        subject_id=row.id,
        metadata={"full_name": repo["full_name"]},
    )
    return row


async def repository_for_project(session: AsyncSession, project_id: uuid.UUID) -> Repository | None:
    return (await session.execute(select(Repository).where(Repository.project_id == project_id))).scalar_one_or_none()


async def job_token(session: AsyncSession, repository: Repository, *, write: bool = False) -> str:
    """Mint a 1-hour installation token scoped to one repository (never sent to the execution plane)."""
    inst = await session.get(GithubInstallation, repository.installation_id)
    if inst is None or inst.suspended_at is not None:
        raise AppError("CONFLICT", "GitHub installation is not active")
    token = await get_client().installation_token(
        inst.installation_id,
        repository_ids=[repository.github_repo_id],
        permissions=PR_PERMISSIONS if write else READ_PERMISSIONS,
    )
    return token.token


async def snapshot(session: AsyncSession, repository: Repository, ref: str) -> tuple[str, bytes]:
    """Resolve ``ref`` to a commit sha and download the repository tarball at that commit."""
    token = await job_token(session, repository)
    client = get_client()
    sha = await client.resolve_ref(token, repository.full_name, ref)
    return sha, await client.download_tarball(token, repository.full_name, sha)


# ------------------------------------------------------------- webhooks ----
def verify_signature(body: bytes, signature: str | None) -> None:
    secret = get_settings().github_webhook_secret
    if secret is None:
        raise AppError("NOT_CONFIGURED", "webhook secret not configured")
    expected = "sha256=" + hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise AppError("UNAUTHENTICATED", "invalid webhook signature")


async def handle_webhook(event: str, delivery_id: str, payload: dict[str, Any]) -> str:
    if not await get_redis().set(f"gh-delivery:{delivery_id}", "1", nx=True, ex=86400):
        return "duplicate"
    installation_id = (payload.get("installation") or {}).get("id")
    action = payload.get("action")
    async with system_tx() as session:
        if event == "installation" and installation_id:
            values: dict[str, Any] | None = None
            if action == "deleted":
                inst = (
                    await session.execute(
                        select(GithubInstallation).where(GithubInstallation.installation_id == installation_id)
                    )
                ).scalar_one_or_none()
                if inst:
                    await session.delete(inst)
                    await audit.record(
                        session,
                        SYSTEM,
                        "github.uninstalled",
                        org_id=inst.org_id,
                        subject_type="github_installation",
                        subject_id=str(installation_id),
                    )
                return "uninstalled"
            if action == "suspend":
                values = {"suspended_at": datetime.now(UTC)}
            elif action == "unsuspend":
                values = {"suspended_at": None}
            if values is not None:
                await session.execute(
                    update(GithubInstallation)
                    .where(GithubInstallation.installation_id == installation_id)
                    .values(**values)
                )
                return f"installation {action}"
        if event == "installation_repositories" and action == "removed":
            removed = [r["id"] for r in payload.get("repositories_removed", [])]
            inst = (
                await session.execute(
                    select(GithubInstallation).where(GithubInstallation.installation_id == installation_id)
                )
            ).scalar_one_or_none()
            if inst and removed:
                repos = (
                    await session.execute(
                        select(Repository).where(
                            Repository.installation_id == inst.id, Repository.github_repo_id.in_(removed)
                        )
                    )
                ).scalars()
                for repo in repos:
                    await session.delete(repo)
                return "repositories removed"
        if event == "pull_request" and action == "closed":
            pr = payload["pull_request"]
            state = "merged" if pr.get("merged") else "closed"
            await session.execute(update(PullRequest).where(PullRequest.url == pr["html_url"]).values(state=state))
            return f"pr {state}"
    return "ignored"
