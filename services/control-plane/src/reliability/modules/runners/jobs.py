from __future__ import annotations

from typing import Any

from reliability.modules.runners.service import reap_expired
from reliability.platform.jobs import job


@job("runners.reap")
async def reap(_: dict[str, Any]) -> None:
    await reap_expired()
