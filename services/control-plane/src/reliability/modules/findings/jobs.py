from __future__ import annotations

import uuid
from typing import Any

from reliability.modules.findings.service import evaluate_run
from reliability.modules.runs import jobs as run_jobs
from reliability.modules.runs.models import Run
from reliability.platform.db import tenant_tx
from reliability.platform.jobs import job

run_jobs.POST_ANALYSIS_TOPICS.append("findings.evaluate")


@job("findings.evaluate")
async def evaluate(payload: dict[str, Any]) -> None:
    async with tenant_tx(org_ids=[uuid.UUID(payload["org_id"])]) as session:
        run = await session.get(Run, uuid.UUID(payload["run_id"]))
        if run is not None:
            await evaluate_run(session, run)
