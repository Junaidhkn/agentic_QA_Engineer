"""Tenant-scoped queries against Mimir (PromQL), Tempo (traces) and Loki (logs) — ADR-0014.

Every request carries ``X-Scope-OrgID`` set from the caller's verified org, never from input.
Backends being unavailable is a normal condition (e.g. customer apps without telemetry): calls
return ``None``/empty and diagnosis records that the evidence was unavailable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)
MAX_RANGE_S = 6 * 3600
MAX_POINTS = 2000


class TelemetryQuery:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, org_id: uuid.UUID) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=get_settings().telemetry_query_timeout_s,
            transport=self._transport,
            headers={"X-Scope-OrgID": str(org_id)},
        )

    async def query_range(
        self, org_id: uuid.UUID, promql: str, start: float, end: float, step_s: int = 15
    ) -> list[dict[str, Any]] | None:
        if end - start > MAX_RANGE_S or (end - start) / max(step_s, 1) > MAX_POINTS:
            raise ValueError("query range too large")
        try:
            async with self._client(org_id) as c:
                resp = await c.get(
                    f"{get_settings().mimir_url}/api/v1/query_range",
                    params={"query": promql, "start": start, "end": end, "step": step_s},
                )
            if resp.status_code != 200:
                return None
            result: list[dict[str, Any]] = resp.json().get("data", {}).get("result", [])
            return result
        except httpx.HTTPError:
            log.info("mimir unavailable")
            return None

    async def get_trace(self, org_id: uuid.UUID, trace_id: str) -> dict[str, Any] | None:
        if not trace_id or not all(c in "0123456789abcdef" for c in trace_id.lower()):
            return None
        try:
            async with self._client(org_id) as c:
                resp = await c.get(
                    f"{get_settings().tempo_url}/api/traces/{trace_id}", headers={"Accept": "application/json"}
                )
            if resp.status_code != 200:
                return None
            data: dict[str, Any] = resp.json()
            return data
        except httpx.HTTPError:
            return None

    async def search_traces(
        self, org_id: uuid.UUID, traceql: str, start: float, end: float, limit: int = 20
    ) -> list[dict[str, Any]] | None:
        try:
            async with self._client(org_id) as c:
                resp = await c.get(
                    f"{get_settings().tempo_url}/api/search",
                    params={"q": traceql, "start": int(start), "end": int(end), "limit": min(limit, 50)},
                )
            if resp.status_code != 200:
                return None
            traces: list[dict[str, Any]] = resp.json().get("traces", [])
            return traces
        except httpx.HTTPError:
            return None

    async def search_logs(
        self, org_id: uuid.UUID, logql: str, start: float, end: float, limit: int = 200
    ) -> list[dict[str, Any]] | None:
        try:
            async with self._client(org_id) as c:
                resp = await c.get(
                    f"{get_settings().loki_url}/loki/api/v1/query_range",
                    params={
                        "query": logql,
                        "start": int(start * 1e9),
                        "end": int(end * 1e9),
                        "limit": min(limit, 500),
                        "direction": "forward",
                    },
                )
            if resp.status_code != 200:
                return None
            lines = []
            for stream in resp.json().get("data", {}).get("result", []):
                for ts, line in stream.get("values", []):
                    lines.append({"ts": int(ts) / 1e9, "line": line[:2000], "labels": stream.get("stream", {})})
            return sorted(lines, key=lambda x: x["ts"])[:limit]
        except httpx.HTTPError:
            return None
