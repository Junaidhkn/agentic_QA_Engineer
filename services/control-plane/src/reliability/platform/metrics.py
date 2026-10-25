"""Platform metrics (operations §6): our own services, never customer evidence.

Prometheus client metrics, scraped by the OTel Collector (``prometheus`` receiver) and sent
to the ``platform`` tenant in Mimir; the SLO rules in ``deploy/observability/rules`` are
written against these names.

* In-process metrics (this module): API RED by route template, job start latency, job
  outcomes, LLM calls/tokens, RLS denials. Processes that fork (uvicorn with several workers,
  Celery prefork) set ``PROMETHEUS_MULTIPROC_DIR`` and the endpoint aggregates the files.
* Database-derived gauges (``modules/ops/metrics.py``): queue and outbox age, runs by status,
  active leases, platform checks. Computed at scrape time by the API only, so every task
  reports the same values — rules use ``max`` over them.

Labels are bounded: route templates (never raw paths), job types, purposes, status classes.
No org ids, user ids or URLs (cost model §8: no high-cardinality labels).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
    start_http_server,
)
from prometheus_client.core import Metric
from prometheus_client.registry import Collector

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0)
# The job-start SLO threshold (30 s) is a bucket boundary so the SLI is exact.
JOB_START_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 120.0, 300.0, 600.0)

HTTP_REQUESTS = Counter(
    "re_http_requests_total", "API requests by route template and status class", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "re_http_request_duration_seconds",
    "API request duration by route template",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
SSE_CONNECTIONS = Gauge("re_sse_connections", "Open server-sent-event streams", multiprocess_mode="livesum")
JOB_START = Histogram(
    "re_job_start_seconds",
    "Queued → started (run: first container running; other jobs: lease acquired), NFR-4",
    ["job_type"],
    buckets=JOB_START_BUCKETS,
)
JOBS = Counter("re_jobs_total", "Control-plane job executions", ["topic", "outcome"])
JOB_DURATION = Histogram(
    "re_job_duration_seconds",
    "Control-plane job duration",
    ["topic"],
    buckets=(0.05, 0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600),
)
KILL_LATENCY = Histogram(
    "re_kill_latency_seconds",
    "Runner-measured cancel received → test tool stopped (NFR-7; excludes ≤ 1 heartbeat interval)",
    ["job_type"],
    buckets=(0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 30.0),
)
LEASES_EXPIRED = Counter("re_runner_leases_expired_total", "Leases reaped after missed heartbeats", ["job_type"])
LLM_REQUESTS = Counter("re_llm_requests_total", "LLM gateway calls", ["purpose", "outcome"])
LLM_TOKENS = Counter("re_llm_tokens_total", "LLM tokens", ["purpose", "kind"])
RLS_DENIALS = Counter("re_rls_denials_total", "Row-level-security denials (must stay 0)")
ADMISSION_REJECTIONS = Counter("re_admission_rejections_total", "Runs rejected by admission", ["code"])


class DbCollector(Collector):
    """Gauges computed from the database: ``refresh`` runs before each scrape (async), ``collect``
    yields the cached values."""

    async def refresh(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


_db_collector: DbCollector | None = None


def status_class(code: int) -> str:
    return f"{code // 100}xx"


def observe_job_start(job_type: str, seconds: float) -> None:
    JOB_START.labels(job_type).observe(max(seconds, 0.0))


def set_db_collector(collector: DbCollector | None) -> None:
    """Database-derived gauges, registered by the API process (``modules/ops``)."""
    global _db_collector
    _db_collector = collector


def _registry() -> CollectorRegistry:
    from prometheus_client import REGISTRY

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


class _Combined(Collector):
    def __init__(self, base: CollectorRegistry, extra: Collector | None) -> None:
        self.base, self.extra = base, extra

    def collect(self) -> Iterable[Metric]:
        yield from self.base.collect()
        if self.extra is not None:
            yield from self.extra.collect()


async def render() -> tuple[bytes, str]:
    if _db_collector is not None:
        await _db_collector.refresh()
    registry = CollectorRegistry()
    registry.register(_Combined(_registry(), _db_collector))
    return generate_latest(registry), CONTENT_TYPE_LATEST


def start_worker_endpoint(port: int) -> None:
    """``/metrics`` for Celery processes (``RE_METRICS_PORT``); aggregates forked children."""
    start_http_server(port, registry=_registry())


def timed_job(topic: str) -> Callable[[float, bool], None]:
    def done(seconds: float, ok: bool) -> None:
        JOBS.labels(topic, "ok" if ok else "error").inc()
        JOB_DURATION.labels(topic).observe(seconds)

    return done
