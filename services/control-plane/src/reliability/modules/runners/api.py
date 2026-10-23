"""Internal runner lease API (served on the PrivateLink-only hostname in production)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from reliability.modules.runners import service
from reliability.modules.runners.auth import RunnerId

router = APIRouter(prefix="/internal/runner/v1", tags=["runner (internal)"], include_in_schema=False)


class AcquireIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key: str = Field(min_length=40, max_length=64, description="base64 X25519 public key")
    job_types: list[str] = Field(min_length=1)
    capacity: dict[str, Any] = Field(default_factory=dict)
    wait_seconds: float | None = Field(default=None, ge=0, le=30)


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["provisioning", "running", "uploading"] | None = None
    progress: dict[str, Any] | None = None


class EventsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[dict[str, Any]] = Field(max_length=200)


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["succeeded", "failed", "aborted", "cancelled", "error"]
    abort_reason: str | None = Field(default=None, max_length=500)
    exit_codes: dict[str, int] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    usage: dict[str, int] = Field(default_factory=dict)
    generator_health: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


@router.post("/leases:acquire", response_model=None)
async def acquire(body: AcquireIn, runner: RunnerId) -> Response:
    lease = await service.acquire(runner, body.public_key, body.job_types, body.wait_seconds)
    if lease is None:
        return Response(status_code=204)
    return JSONResponse(lease)


@router.post("/leases/{lease_id}:heartbeat")
async def heartbeat(lease_id: uuid.UUID, body: HeartbeatIn, runner: RunnerId) -> dict[str, Any]:
    return await service.heartbeat(runner, lease_id, body.phase, body.progress)


@router.post("/leases/{lease_id}/events")
async def events(lease_id: uuid.UUID, body: EventsIn, runner: RunnerId) -> dict[str, int]:
    return {"accepted": await service.post_events(runner, lease_id, body.events)}


@router.post("/leases/{lease_id}:complete", status_code=204)
async def complete(lease_id: uuid.UUID, body: CompleteIn, runner: RunnerId) -> Response:
    await service.complete(runner, lease_id, body.model_dump())
    return Response(status_code=204)
