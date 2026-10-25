"""OTLP ingest gateway (process type: ``otlp-gateway``) — ADR-0014, api-contract §8.

Authenticates the bearer token, derives the tenant, rate-limits per org, and forwards the
raw OTLP payload to the internal OpenTelemetry Collector with ``X-Scope-OrgID`` set. Any
tenant header sent by the client is discarded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, Request, Response

from reliability.modules.quotas import ratelimit
from reliability.modules.telemetry.ingest import authenticate
from reliability.platform.errors import AppError, install_error_handlers
from reliability.platform.settings import get_settings

MAX_BODY = 8 * 1024 * 1024
FORWARD_HEADERS = ("content-type", "content-encoding")
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _client
    _client = httpx.AsyncClient(timeout=10.0)
    yield
    await _client.aclose()


app = FastAPI(title="OTLP gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
install_error_handlers(app)
ratelimit.CLASSES.setdefault("ingest", ratelimit.Limit(capacity=2000, per_seconds=1, fail_closed=False))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/{signal}")
async def ingest(signal: str, request: Request, authorization: Annotated[str | None, Header()] = None) -> Response:
    if signal not in ("traces", "metrics", "logs"):
        raise AppError("NOT_FOUND", "unknown signal")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("UNAUTHENTICATED", "bearer token required")
    identity = await authenticate(authorization[7:].strip())
    await ratelimit.check(str(identity.org_id), "ingest")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise AppError("VALIDATION_FAILED", "payload too large")
    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
    headers["X-Scope-OrgID"] = str(identity.org_id)
    assert _client is not None
    try:
        resp = await _client.post(f"{get_settings().otel_collector_url}/v1/{signal}", content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise AppError("UNAVAILABLE", "telemetry backend unavailable", headers={"Retry-After": "5"}) from exc
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/x-protobuf"),
    )
