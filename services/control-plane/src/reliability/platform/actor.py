"""Who performed an action — attached to audit events and authorization decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActorType = Literal["user", "api_key", "system", "agent", "operator", "runner"]


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: str
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


SYSTEM = Actor("system", "system")
