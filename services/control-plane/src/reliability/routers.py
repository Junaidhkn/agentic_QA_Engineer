"""Registers every module's API router under ``/v1``. Extended as modules are added."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from reliability.platform import local_store_api
from reliability.platform.settings import get_settings


def include_routers(app: FastAPI) -> None:
    v1 = APIRouter(prefix="/v1")
    if get_settings().object_store == "local":
        v1.include_router(local_store_api.router)
    app.include_router(v1)
