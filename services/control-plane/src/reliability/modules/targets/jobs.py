from __future__ import annotations

from typing import Any

from reliability.modules.targets.service import reverify_due
from reliability.platform.jobs import job


@job("targets.reverify_sweep")
async def reverify_sweep(_: dict[str, Any]) -> None:
    await reverify_due()


@job("targets.revoked")
async def target_revoked(payload: dict[str, Any]) -> None:
    """Abort in-flight runs against a target whose verification was revoked or failed (FR-4)."""
    import importlib

    runs = importlib.import_module("reliability.modules.runs.service")
    await runs.cancel_runs_for_target(payload["target_id"], reason="target_verification_revoked")
