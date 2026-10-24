"""FastAPI application factory (process type: ``api``)."""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from reliability.platform import db, metrics, redis_client
from reliability.platform.errors import install_error_handlers
from reliability.platform.ids import uuid7
from reliability.platform.logging import configure_logging, request_id_var
from reliability.platform.settings import get_settings
from reliability.routers import include_routers

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Cache-Control": "no-store",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await db.dispose_engines()
    await redis_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Reliability Engineer API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env in ("local", "test") else None,
        redoc_url=None,
    )
    install_error_handlers(app)
    from reliability.modules.ops.metrics import DatabaseCollector

    metrics.set_db_collector(DatabaseCollector())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Org-Id", "Last-Event-ID"],
        expose_headers=["X-Request-Id", "Retry-After"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-Id") or f"req_{uuid7().hex}"
        request.state.request_id = rid
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            request_id_var.reset(token)
            _observe(request, status, time.perf_counter() - started)
        response.headers["X-Request-Id"] = rid
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        # Internal only: the ALB never routes /metrics; the token is the second lock.
        expected = settings.metrics_token
        if expected is None and settings.env not in ("local", "test"):
            return Response(status_code=404)
        if expected is not None:
            given = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(given.encode(), expected.get_secret_value().encode()):
                return Response(status_code=404)
        body, content_type = await metrics.render()
        return Response(body, media_type=content_type, headers={"Cache-Control": "no-store"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        checks = {"postgres": await db.ping(), "redis": await redis_client.ping()}
        ok = all(checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)

    include_routers(app)
    return app


UNOBSERVED = {"/metrics", "/healthz", "/readyz"}


def route_template(scope: MutableMapping[str, Any]) -> str:
    """``/v1/projects/0192…`` → ``/v1/projects/{project_id}``. Included routers keep only their own
    path on the matched route, so the template is rebuilt from the request path and its params."""
    if scope.get("route") is None:
        return "unmatched"
    path: str = scope.get("path", "")
    for name, value in (scope.get("path_params") or {}).items():
        path = re.sub(rf"(?<=[/:]){re.escape(str(value))}(?=$|[/:])", "{" + name + "}", path)
    return path


def _observe(request: Request, status: int, seconds: float) -> None:
    template = route_template(request.scope)
    if template in UNOBSERVED:
        return
    metrics.HTTP_REQUESTS.labels(request.method, template, metrics.status_class(status)).inc()
    metrics.HTTP_DURATION.labels(request.method, template).observe(seconds)


app = create_app()
