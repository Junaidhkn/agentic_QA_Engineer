"""Celery application (process types: ``worker-default``, ``worker-agents``, ``worker-github``, ``scheduler``).

Every task is a thin wrapper that runs a registered async job handler (platform.jobs) in a
fresh event loop. Messages carry ids only; Postgres state machines are the source of truth
(ADR-0004).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from reliability.platform import db
from reliability.platform.jobs import all_jobs, get_job
from reliability.platform.logging import configure_logging
from reliability.platform.settings import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("reliability", broker=settings.broker_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    broker_transport_options={"visibility_timeout": 3 * 3600},
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "audit-anchor": {
            "task": "reliability.run_job",
            "args": ["audit.anchor", {}],
            "schedule": crontab(minute=5, hour=0),
        },
        "targets-reverify": {
            "task": "reliability.run_job",
            "args": ["targets.reverify_sweep", {}],
            "schedule": crontab(minute=7),
        },
        "partitions": {
            "task": "reliability.run_job",
            "args": ["maintenance.partitions", {}],
            "schedule": crontab(minute=15, hour=1),
        },
    },
)


@worker_process_init.connect
def _init_worker(**_: Any) -> None:
    configure_logging(settings.log_level)
    db.use_nullpool(True)


@celery_app.task(name="reliability.run_job", bind=True, max_retries=5)
def run_job(self: Any, topic: str, payload: dict[str, Any]) -> None:
    spec = get_job(topic)
    try:
        asyncio.run(_run(spec.handler, payload))
    except Exception as exc:
        log.exception("job failed", extra={"topic": topic, "retries": self.request.retries})
        if self.request.retries >= spec.max_retries:
            raise
        raise self.retry(exc=exc, countdown=min(2**self.request.retries * 5, 300)) from exc


async def _run(handler: Any, payload: dict[str, Any]) -> None:
    try:
        await handler(payload)
    finally:
        await db.dispose_engines()


def queue_for(topic: str) -> str:
    return all_jobs()[topic].queue


def publish(topic: str, payload: dict[str, Any]) -> None:
    run_job.apply_async(args=[topic, payload], queue=queue_for(topic))
