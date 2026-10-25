from __future__ import annotations

from typing import Any

from reliability.modules.audit.service import anchor_all
from reliability.platform.jobs import job


@job("audit.anchor")
async def anchor(_: dict[str, Any]) -> None:
    await anchor_all()
