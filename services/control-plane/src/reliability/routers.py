"""Registers every module's API router under ``/v1``. Extended as modules are added."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI


def include_routers(app: FastAPI) -> None:
    v1 = APIRouter(prefix="/v1")
    app.include_router(v1)
