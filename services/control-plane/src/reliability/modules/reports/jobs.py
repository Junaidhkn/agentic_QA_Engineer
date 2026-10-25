"""Report generation job."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from reliability.modules.reports import service
from reliability.modules.reports.models import Report
from reliability.platform.db import tenant_tx
from reliability.platform.jobs import job

log = logging.getLogger(__name__)


@job("reports.generate")
async def generate(payload: dict[str, Any]) -> None:
    org_id, report_id = uuid.UUID(payload["org_id"]), uuid.UUID(payload["report_id"])
    try:
        async with tenant_tx(org_ids=[org_id]) as s:
            report = await s.get(Report, report_id, with_for_update=True)
            if report is None or report.status != "queued":
                return
            await service.generate(s, report)
    except Exception:
        log.exception("report generation failed", extra={"report_id": str(report_id)})
        async with tenant_tx(org_ids=[org_id]) as s:
            report = await s.get(Report, report_id, with_for_update=True)
            if report is not None and report.status == "queued":
                report.status = "failed"
