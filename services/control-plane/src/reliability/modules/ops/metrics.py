"""Database-derived platform gauges (operations §6: queues, runs, execution plane, checks).

Computed by the API at scrape time (cached for a few seconds so several scrapers don't each
hit Postgres) from the system role. Every query is an aggregate: counts and ages, labelled by
bounded values only (job types, statuses, check names).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from prometheus_client.core import GaugeMetricFamily, Metric
from sqlalchemy import text

from reliability.modules.ops import checks
from reliability.platform.db import system_tx
from reliability.platform.flags import KILL_SWITCH, is_enabled
from reliability.platform.metrics import DbCollector

CACHE_S = 5.0
NON_TERMINAL = ("pending_approval", "queued", "provisioning", "running", "cancelling", "analyzing")

QUEUES = """
SELECT 'run:' || kind AS queue, count(*) AS depth,
       coalesce(extract(epoch FROM now() - min(queued_at)), 0) AS oldest
FROM runs WHERE status = 'queued' GROUP BY kind
UNION ALL
SELECT 'job:' || job_type, count(*), coalesce(extract(epoch FROM now() - min(queued_at)), 0)
FROM execution_jobs WHERE status = 'queued' GROUP BY job_type
"""
OUTBOX = """
SELECT count(*) FILTER (WHERE dispatched_at IS NULL AND failed_at IS NULL) AS pending,
       coalesce(extract(epoch FROM now() - min(available_at)
         FILTER (WHERE dispatched_at IS NULL AND failed_at IS NULL AND available_at <= now())), 0) AS oldest,
       count(*) FILTER (WHERE failed_at > now() - interval '24 hours') AS failed_24h
FROM outbox
"""
RUNS = "SELECT status, count(*) FROM runs WHERE status = ANY(:s) GROUP BY status"
LEASES = """
SELECT job_type, count(*) AS active,
       coalesce(max(extract(epoch FROM now() - coalesce(last_heartbeat_at, created_at))), 0) AS lag
FROM runner_leases WHERE state = 'active' GROUP BY job_type
"""


class DatabaseCollector(DbCollector):
    def __init__(self) -> None:
        self._families: list[Metric] = []
        self._at = 0.0

    async def refresh(self) -> None:
        if time.monotonic() - self._at < CACHE_S and self._families:
            return
        try:
            self._families = await self._query()
            up = 1.0
        except Exception:  # the endpoint must still answer when Postgres is down
            self._families, up = [], 0.0
        self._families.append(_gauge("re_metrics_db_up", "Database-derived gauges collected", [((), up)]))
        self._at = time.monotonic()

    def collect(self) -> Iterable[Metric]:
        yield from self._families

    async def _query(self) -> list[Metric]:
        async with system_tx() as s:
            queues = (await s.execute(text(QUEUES))).all()
            outbox = (await s.execute(text(OUTBOX))).one()
            runs = (await s.execute(text(RUNS), {"s": list(NON_TERMINAL)})).all()
            leases = (await s.execute(text(LEASES))).all()
            all_checks = await checks.all_checks(s)
            kill = await is_enabled(s, KILL_SWITCH, None)
        by_status = {r[0]: r[1] for r in runs}
        fams: list[Metric] = [
            _gauge("re_queue_depth", "Queued runs and runner jobs", [((q.queue,), q.depth) for q in queues], ["queue"]),
            _gauge(
                "re_queue_oldest_age_seconds",
                "Age of the oldest queued item (job-start SLO early warning)",
                [((q.queue,), q.oldest) for q in queues],
                ["queue"],
            ),
            _gauge("re_outbox_pending", "Outbox rows not yet published", [((), outbox.pending)]),
            _gauge("re_outbox_oldest_age_seconds", "Age of the oldest due outbox row", [((), outbox.oldest)]),
            _gauge("re_outbox_failed_24h", "Outbox rows that exhausted retries in 24 h", [((), outbox.failed_24h)]),
            _gauge(
                "re_runs_active", "Runs by non-terminal status", [((st,), by_status.get(st, 0)) for st in NON_TERMINAL]
            ),
            _gauge(
                "re_runner_leases_active",
                "Active runner leases",
                [((r.job_type,), r.active) for r in leases],
                ["job_type"],
            ),
            _gauge(
                "re_runner_heartbeat_lag_seconds",
                "Longest time since an active lease's last heartbeat",
                [((r.job_type,), r.lag) for r in leases],
                ["job_type"],
            ),
            _gauge("re_kill_switch_active", "Global kill switch", [((), 1.0 if kill else 0.0)]),
        ]
        now = time.time()
        fams += [
            _gauge("re_platform_check_ok", "Latest outcome (1 ok, 0 failed)", _per_check(all_checks, _ok), ["check"]),
            _gauge(
                "re_platform_check_last_success_timestamp_seconds",
                "Unix time of the last successful check",
                [((c.name,), c.last_success_at.timestamp()) for c in all_checks if c.last_success_at],
                ["check"],
            ),
            _gauge(
                "re_platform_check_age_seconds",
                "Seconds since the check last ran",
                [((c.name,), now - c.checked_at.timestamp()) for c in all_checks],
                ["check"],
            ),
            _gauge(
                "re_platform_check_consecutive_failures",
                "Consecutive failed runs of the check",
                _per_check(all_checks, lambda c: float(c.consecutive_failures)),
                ["check"],
            ),
        ]
        integrity = next((c for c in all_checks if c.name == checks.INTEGRITY), None)
        if integrity is not None:
            violations = dict(integrity.detail.get("invariants") or {})
            violations["audit_chain_broken"] = len(integrity.detail.get("broken_chains") or [])
            fams.append(
                _gauge(
                    "re_integrity_violations",
                    "Rows violating a platform invariant at the last integrity check (must be 0)",
                    [((k,), float(v)) for k, v in sorted(violations.items())],
                    ["invariant"],
                )
            )
        return fams


def _ok(c: checks.Check) -> float:
    return 1.0 if c.status == "ok" else 0.0


def _per_check(items: list[checks.Check], fn: Any) -> list[tuple[tuple[str, ...], float]]:
    return [((c.name,), fn(c)) for c in items]


def _gauge(
    name: str, doc: str, samples: list[tuple[tuple[Any, ...], Any]], labels: list[str] | None = None
) -> GaugeMetricFamily:
    fam = GaugeMetricFamily(name, doc, labels=labels or [])
    for values, value in samples:
        fam.add_metric([str(v) for v in values], float(value))
    return fam
