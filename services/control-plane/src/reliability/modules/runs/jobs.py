from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete

from reliability.modules.artifacts import service as artifacts
from reliability.modules.runs import analysis, service
from reliability.modules.runs.models import RunMetricSummary
from reliability.platform import outbox
from reliability.platform.db import tenant_tx
from reliability.platform.jobs import job

log = logging.getLogger(__name__)
POST_ANALYSIS_TOPICS: list[str] = []  # extended by later phases (e.g. findings.evaluate)


def _num(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(round(v, 3)))


@job("runs.analyze")
async def analyze(payload: dict[str, Any]) -> None:
    run_id, org_id = uuid.UUID(payload["run_id"]), uuid.UUID(payload["org_id"])
    async with tenant_tx(org_ids=[org_id]) as session:
        run = await service.lock_for_update(session, run_id)
        if run.status != "analyzing":
            return
        arts = {a.kind + ":" + a.storage_key.rsplit("/", 1)[1]: a for a in await artifacts.for_run(session, run.id)}
        execution = run.config_snapshot.get("_execution") or {}
        started = run.started_at or run.queued_at or run.created_at
        duration = (datetime.now(UTC) - started).total_seconds()

        def load_json(name: str) -> dict[str, Any] | None:
            art = arts.get(name)
            if art is None:
                return None
            try:
                parsed: dict[str, Any] = json.loads(artifacts.read(art))
                return parsed
            except (ValueError, OSError):
                log.warning("unreadable artifact", extra={"artifact": name})
                return None

        k6_summary = load_json("k6_summary:summary.json")
        k6 = analysis.parse_k6_summary(k6_summary) if k6_summary else None
        if run.kind == "load":
            if k6 is None:
                await service.transition(session, run, "errored", "k6 summary missing")
                await service.settle_usage(session, run)
                return
            verdict = analysis.load_verdict(
                k6, dict(run.generator_health or {}), execution.get("abort_reason"), duration
            )
        elif run.kind == "probe":
            probes = load_json("k6_summary:probes.json") or {}
            failed = [name for name, result in probes.items() if not result.get("passed")]
            verdict = analysis.Verdict(
                "failed" if failed else "passed",
                f"invariant violated: {', '.join(failed)}" if failed else None,
                {"probes": probes},
            )
        else:
            pw_report = load_json("playwright_report:report.json")
            pw = analysis.parse_playwright_report(pw_report) if pw_report else None
            if pw is None and k6 is None:
                await service.transition(session, run, "errored", "no test results were produced")
                await service.settle_usage(session, run)
                return
            verdict = analysis.functional_verdict(pw, k6)

        if k6 is not None:
            await session.execute(delete(RunMetricSummary).where(RunMetricSummary.run_id == run.id))
            for scope, stats in k6.scopes.items():
                rps = k6.rps if scope == "overall" else (stats.requests / duration if duration else 0)
                session.add(
                    RunMetricSummary(
                        run_id=run.id,
                        org_id=run.org_id,
                        scope=scope,
                        time_window="full",
                        requests=stats.requests,
                        rps=Decimal(str(round(rps, 3))),
                        error_rate=Decimal(str(round(stats.error_rate, 5))),
                        p50_ms=_num(stats.p50_ms),
                        p90_ms=_num(stats.p90_ms),
                        p95_ms=_num(stats.p95_ms),
                        p99_ms=_num(stats.p99_ms),
                        max_ms=_num(stats.max_ms),
                    )
                )
        run.summary = verdict.summary
        run.generator_health = verdict.summary.get("generator", run.generator_health)
        await service.transition(session, run, verdict.status, verdict.reason)
        await service.settle_usage(session, run)
        for topic in POST_ANALYSIS_TOPICS:
            await outbox.enqueue(session, topic, {"run_id": str(run.id), "org_id": str(run.org_id)}, org_id=run.org_id)
