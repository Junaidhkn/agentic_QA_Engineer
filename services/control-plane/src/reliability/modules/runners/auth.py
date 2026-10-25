"""Runner identity. In production the lease API is reachable only over PrivateLink with mTLS
terminated at the load balancer; the app additionally requires a signed runner token
(``reliability issue-runner-token``) bound to the host's runner id."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from reliability.modules.quotas import ratelimit
from reliability.platform.crypto import sign_token, verify_token
from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

AUDIENCE = "runner"


def issue_runner_token(runner_id: str, ttl_seconds: int = 86400) -> str:
    return sign_token(
        {"runner_id": runner_id}, AUDIENCE, ttl_seconds, key=get_settings().runner_signing_key.get_secret_value()
    )


async def runner_id(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("UNAUTHENTICATED", "runner token required")
    claims = verify_token(authorization[7:].strip(), AUDIENCE, key=get_settings().runner_signing_key.get_secret_value())
    rid = str(claims.get("runner_id") or "")
    if not rid:
        raise AppError("UNAUTHENTICATED", "invalid runner token")
    await ratelimit.check(rid, "runner")
    return rid


RunnerId = Annotated[str, Depends(runner_id)]
