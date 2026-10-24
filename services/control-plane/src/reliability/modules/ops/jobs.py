from __future__ import annotations

from typing import Any

from reliability.modules.ops import canary, checks, integrity
from reliability.platform.db import system_tx
from reliability.platform.jobs import job


async def check_integrity() -> dict[str, Any]:
    async with system_tx() as s:
        result = await integrity.run_checks(s)
        await checks.record(s, checks.INTEGRITY, bool(result["ok"]), result)
    return result


@job("ops.integrity")
async def integrity_job(_: dict[str, Any]) -> None:
    await check_integrity()


@job("ops.canary", max_retries=0)
async def canary_job(_: dict[str, Any]) -> None:
    await canary.run()
