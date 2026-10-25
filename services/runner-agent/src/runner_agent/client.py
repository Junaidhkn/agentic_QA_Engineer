"""Lease API client."""

from __future__ import annotations

from typing import Any

import httpx

from runner_agent.config import Config


class LeaseClient:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=config.api_url.rstrip("/") + "/internal/runner/v1",
            headers={"Authorization": f"Bearer {config.runner_token}", "User-Agent": f"runner/{config.runner_id}"},
            timeout=config.http_timeout_s,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def acquire(
        self, public_key: str, job_types: list[str], capacity: dict[str, Any], wait_seconds: float = 20
    ) -> dict[str, Any] | None:
        resp = await self._client.post(
            "/leases:acquire",
            json={
                "public_key": public_key,
                "job_types": job_types,
                "capacity": capacity,
                "wait_seconds": wait_seconds,
            },
            timeout=wait_seconds + 15,
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def heartbeat(self, lease_id: str, phase: str | None, progress: dict[str, Any] | None) -> dict[str, Any]:
        resp = await self._client.post(f"/leases/{lease_id}:heartbeat", json={"phase": phase, "progress": progress})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def events(self, lease_id: str, events: list[dict[str, Any]]) -> None:
        if events:
            (await self._client.post(f"/leases/{lease_id}/events", json={"events": events})).raise_for_status()

    async def complete(self, lease_id: str, body: dict[str, Any]) -> None:
        (await self._client.post(f"/leases/{lease_id}:complete", json=body)).raise_for_status()

    async def download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=120) as c:
            resp = await c.get(url, follow_redirects=True)
            resp.raise_for_status()
            return resp.content

    async def upload(self, url: str, data: bytes, content_type: str) -> None:
        async with httpx.AsyncClient(timeout=300) as c:
            resp = await c.put(url, content=data, headers={"Content-Type": content_type})
            resp.raise_for_status()
