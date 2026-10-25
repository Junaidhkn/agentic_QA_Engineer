"""Transactional outbox (ADR-0004).

Services call :func:`enqueue` inside the same transaction that changes state. The relay
(:func:`relay_once` / :func:`run_relay`) publishes undispatched rows to Celery. A rollback
therefore never leaves a phantom job, and a crash after commit never loses one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from reliability.platform.db import Base, system_tx

log = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class OutboxRow(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json.dumps(payload, default=str))
    return result


async def enqueue(
    session: AsyncSession,
    topic: str,
    payload: dict[str, Any],
    *,
    org_id: uuid.UUID | None = None,
    delay_seconds: float = 0,
) -> None:
    """Record a job to run after the current transaction commits.

    Uses a plain INSERT (no RETURNING): the app role may insert into the outbox but not read it.
    """
    await session.execute(
        text(
            "INSERT INTO outbox (topic, payload, org_id, available_at) "
            "VALUES (:t, CAST(:p AS jsonb), :o, now() + make_interval(secs => :d))"
        ),
        {"t": topic, "p": json.dumps(_jsonable(payload)), "o": org_id, "d": delay_seconds},
    )


async def relay_once(publish: Publisher, batch_size: int = 100, max_attempts: int = 10) -> int:
    """Publish one batch of pending rows. Returns how many were dispatched."""
    dispatched = 0
    async with system_tx() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, topic, payload, attempts FROM outbox
                    WHERE dispatched_at IS NULL AND failed_at IS NULL AND available_at <= now()
                    ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED
                    """
                ),
                {"n": batch_size},
            )
        ).all()
        for row in rows:
            try:
                maybe = publish(row.topic, row.payload)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:  # broker down: retry later, give up after max_attempts
                attempts = row.attempts + 1
                await session.execute(
                    text(
                        """
                        UPDATE outbox SET attempts = :a, last_error = :e,
                          available_at = now() + make_interval(secs => :backoff),
                          failed_at = CASE WHEN :a >= :max THEN now() END
                        WHERE id = :id
                        """
                    ),
                    {
                        "a": attempts,
                        "e": str(exc)[:500],
                        "backoff": min(2**attempts, 300),
                        "max": max_attempts,
                        "id": row.id,
                    },
                )
                log.warning("outbox publish failed", extra={"outbox_id": row.id, "topic": row.topic})
                continue
            await session.execute(text("UPDATE outbox SET dispatched_at = now() WHERE id = :id"), {"id": row.id})
            dispatched += 1
    return dispatched


async def lag_seconds() -> float:
    async with system_tx() as session:
        value = await session.scalar(
            text(
                "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM outbox "
                "WHERE dispatched_at IS NULL AND failed_at IS NULL AND available_at <= now()"
            )
        )
    return float(value or 0)


async def run_relay(publish: Publisher, idle_sleep: float = 0.5) -> None:
    log.info("outbox relay started")
    while True:
        try:
            n = await relay_once(publish)
        except Exception:
            log.exception("outbox relay iteration failed")
            n = 0
        if n == 0:
            await asyncio.sleep(idle_sleep)


def batch_collector(sink: list[tuple[str, dict[str, Any]]]) -> Publisher:
    def collect(topic: str, payload: dict[str, Any]) -> None:
        sink.append((topic, payload))

    return collect


async def drain_in_process(max_rounds: int = 50) -> list[str]:
    """Test/dev helper: run pending jobs in this process until the outbox is empty.

    Returns the topics executed, in order. Jobs enqueued by jobs are executed too.
    """
    from reliability.platform.jobs import get_job

    executed: list[str] = []
    for _ in range(max_rounds):
        batch: list[tuple[str, dict[str, Any]]] = []

        await relay_once(batch_collector(batch))
        if not batch:
            return executed
        for topic, payload in batch:
            await get_job(topic).handler(payload)
            executed.append(topic)
    raise RuntimeError("outbox did not drain")
