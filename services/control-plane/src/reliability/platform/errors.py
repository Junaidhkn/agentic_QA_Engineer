"""One error envelope for the whole API (api-contract.md §2)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

# code -> HTTP status
ERROR_STATUS: dict[str, int] = {
    "BAD_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "ENVIRONMENT_POLICY_VIOLATION": 403,
    "ORG_SUSPENDED": 403,
    "CODE_CONTEXT_DISABLED": 403,
    "NOT_FOUND": 404,
    "TARGET_NOT_VERIFIED": 409,
    "INVALID_STATE": 409,
    "CONCURRENCY_LIMIT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "CONFLICT": 409,
    "VALIDATION_FAILED": 422,
    "VERIFICATION_FAILED": 422,
    "UNSAFE_TARGET": 422,
    "PLAN_LIMIT_EXCEEDED": 422,
    "QUOTA_EXCEEDED": 429,
    "RATE_LIMITED": 429,
    "UNAVAILABLE": 503,
    "NOT_CONFIGURED": 503,
}


class AppError(Exception):
    """A domain error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}

    @property
    def status(self) -> int:
        return ERROR_STATUS[self.code]


def is_rls_denial(exc: BaseException) -> bool:
    """A query refused by a row-level-security policy: a tenancy bug, alerted on at any rate."""
    text = str(exc).lower()
    return "row-level security" in text or "row level security" in text


def not_found(kind: str = "resource") -> AppError:
    return AppError("NOT_FOUND", f"{kind} not found")


def _envelope(request: Request, code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            _envelope(request, exc.code, exc.message, exc.details),
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"loc": [str(p) for p in e.get("loc", [])], "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            _envelope(request, "VALIDATION_FAILED", "request validation failed", {"fields": fields}),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(exc.status_code, "BAD_REQUEST")
        return JSONResponse(_envelope(request, code, str(exc.detail), {}), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        if is_rls_denial(exc):
            from reliability.platform.metrics import RLS_DENIALS

            RLS_DENIALS.inc()
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(_envelope(request, "UNAVAILABLE", "internal error", {}), status_code=500)
