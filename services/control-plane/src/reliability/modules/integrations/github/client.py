"""Minimal GitHub App client (ADR-0017).

Only the calls the product needs: installation tokens (down-scoped per job), repository
listing, ref resolution, tarball download, and *creating* a branch, commit and pull request.
There is deliberately no merge, no default-branch update and no repository administration.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
BRANCH_PREFIX = "reliability/"


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str


class GitHubClient:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self._key = private_key
        self.api_url = api_url.rstrip("/")
        self._transport = transport

    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self._key, algorithm="RS256")

    def _client(self, token: str, bearer: bool = True) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_url,
            timeout=10.0,
            transport=self._transport,
            headers={
                "Authorization": f"{'Bearer' if bearer else 'token'} {token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "reliability-engineer",
            },
        )

    async def _json(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            raise AppError(
                "UNAVAILABLE" if resp.status_code >= 500 else "BAD_REQUEST",
                f"GitHub API error {resp.status_code}",
                {"github": resp.text[:300]},
            )
        return resp.json() if resp.content else None

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        async with self._client(self.app_jwt()) as c:
            data: dict[str, Any] = await self._json(await c.get(f"/app/installations/{installation_id}"))
            return data

    async def installation_token(
        self, installation_id: int, repository_ids: list[int] | None = None, permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        body: dict[str, Any] = {}
        if repository_ids:
            body["repository_ids"] = repository_ids
        if permissions:
            body["permissions"] = permissions
        async with self._client(self.app_jwt()) as c:
            data = await self._json(await c.post(f"/app/installations/{installation_id}/access_tokens", json=body))
        return InstallationToken(data["token"], data["expires_at"])

    async def list_repositories(self, token: str) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        async with self._client(token) as c:
            for page in range(1, 11):
                data = await self._json(
                    await c.get("/installation/repositories", params={"per_page": 100, "page": page})
                )
                repos.extend(data["repositories"])
                if len(data["repositories"]) < 100:
                    break
        return repos

    async def resolve_ref(self, token: str, full_name: str, ref: str) -> str:
        async with self._client(token) as c:
            data = await self._json(await c.get(f"/repos/{full_name}/commits/{ref}"))
        return str(data["sha"])

    async def download_tarball(self, token: str, full_name: str, sha: str, max_bytes: int = 200 * 1024 * 1024) -> bytes:
        async with (
            self._client(token) as c,
            c.stream("GET", f"/repos/{full_name}/tarball/{sha}", follow_redirects=True) as resp,
        ):
            if resp.status_code >= 400:
                raise AppError("BAD_REQUEST", f"tarball download failed ({resp.status_code})")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise AppError("VALIDATION_FAILED", "repository archive exceeds size limit")
                chunks.append(chunk)
        return b"".join(chunks)

    async def create_branch_with_changes(
        self, token: str, full_name: str, base_sha: str, branch: str, files: dict[str, str | None], message: str
    ) -> str:
        """Create ``branch`` (must start with ``reliability/``) with one commit applying ``files``.

        ``files`` maps path -> new content, or None to delete. Returns the new commit sha.
        """
        if not branch.startswith(BRANCH_PREFIX):
            raise ValueError("branches created by the platform must use the reliability/ prefix")
        async with self._client(token) as c:
            base_commit = await self._json(await c.get(f"/repos/{full_name}/git/commits/{base_sha}"))
            tree = []
            for path, content in files.items():
                if content is None:
                    tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                    continue
                blob = await self._json(
                    await c.post(
                        f"/repos/{full_name}/git/blobs",
                        json={"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"},
                    )
                )
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            new_tree = await self._json(
                await c.post(
                    f"/repos/{full_name}/git/trees", json={"base_tree": base_commit["tree"]["sha"], "tree": tree}
                )
            )
            commit = await self._json(
                await c.post(
                    f"/repos/{full_name}/git/commits",
                    json={"message": message, "tree": new_tree["sha"], "parents": [base_sha]},
                )
            )
            await self._json(
                await c.post(f"/repos/{full_name}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
            )
        return str(commit["sha"])

    async def open_pull_request(
        self, token: str, full_name: str, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        if not head.startswith(BRANCH_PREFIX):
            raise ValueError("pull requests must come from a reliability/ branch")
        async with self._client(token) as c:
            data: dict[str, Any] = await self._json(
                await c.post(
                    f"/repos/{full_name}/pulls",
                    json={
                        "title": title,
                        "head": head,
                        "base": base,
                        "body": body,
                        "draft": False,
                        "maintainer_can_modify": True,
                    },
                )
            )
        return data

    @staticmethod
    async def user_installations(
        user_token: str, api_url: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> list[int]:
        async with httpx.AsyncClient(
            base_url=api_url,
            timeout=10.0,
            transport=transport,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
            },
        ) as c:
            resp = await c.get("/user/installations", params={"per_page": 100})
            if resp.status_code >= 400:
                raise AppError("FORBIDDEN", "could not verify GitHub installation access")
            return [int(i["id"]) for i in resp.json()["installations"]]


_override: GitHubClient | None = None


def set_client_override(client: GitHubClient | None) -> None:
    """Tests inject a client with a mock transport."""
    global _override
    _override = client


def get_client() -> GitHubClient:
    if _override is not None:
        return _override
    s = get_settings()
    if not (s.github_app_id and s.github_private_key):
        raise AppError("NOT_CONFIGURED", "GitHub App is not configured (RE_GITHUB_APP_ID, RE_GITHUB_PRIVATE_KEY)")
    return GitHubClient(s.github_app_id, s.github_private_key.get_secret_value().replace("\\n", "\n"), s.github_api_url)
