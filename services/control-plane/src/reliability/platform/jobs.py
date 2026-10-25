"""Background job registry.

Jobs are async handlers keyed by an outbox *topic*. Producers never call Celery directly:
they write an outbox row inside their DB transaction (ADR-0004) and the relay publishes it.
Handlers receive ids only and must be idempotent.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]

QUEUES = ("default", "agents", "github")


@dataclass(frozen=True)
class JobSpec:
    topic: str
    handler: JobHandler
    queue: str
    max_retries: int


_REGISTRY: dict[str, JobSpec] = {}


def job(topic: str, queue: str = "default", max_retries: int = 3) -> Callable[[JobHandler], JobHandler]:
    if queue not in QUEUES:
        raise ValueError(f"unknown queue {queue}")

    def register(fn: JobHandler) -> JobHandler:
        if topic in _REGISTRY and _REGISTRY[topic].handler is not fn:
            raise ValueError(f"duplicate job topic {topic}")
        _REGISTRY[topic] = JobSpec(topic, fn, queue, max_retries)
        return fn

    return register


def get_job(topic: str) -> JobSpec:
    load_all_jobs()
    try:
        return _REGISTRY[topic]
    except KeyError as exc:
        raise KeyError(f"no job registered for topic {topic}") from exc


def all_jobs() -> dict[str, JobSpec]:
    load_all_jobs()
    return dict(_REGISTRY)


def load_all_jobs() -> None:
    """Import every module's ``jobs`` so their handlers register (plugin-style, by name, so
    ``platform`` keeps no static dependency on ``modules``)."""
    importlib.import_module("reliability.jobs_index")
