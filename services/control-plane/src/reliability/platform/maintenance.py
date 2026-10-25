"""Partition maintenance for append-heavy tables (data-model.md §1)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from reliability.platform.jobs import job
from reliability.platform.settings import get_settings

PARTITIONED = ("run_events", "audit_events", "usage_ledger", "llm_usage")


def _month_start(d: date, offset: int) -> date:
    months = d.year * 12 + d.month - 1 + offset
    return date(months // 12, months % 12 + 1, 1)


async def ensure_partitions(months_ahead: int = 3, today: date | None = None) -> list[str]:
    """Create monthly partitions for the next ``months_ahead`` months (current month stays in DEFAULT)."""
    today = today or datetime.now(UTC).date()
    created: list[str] = []
    # DDL needs the schema owner; the runtime roles cannot create tables.
    engine = create_async_engine(get_settings().database_migration_url, poolclass=NullPool)
    async with engine.begin() as session:
        for table in PARTITIONED:
            for offset in range(1, months_ahead + 1):
                start, end = _month_start(today, offset), _month_start(today, offset + 1)
                name = f"{table}_{start:%Y%m}"
                exists = await session.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name})
                if exists:
                    continue
                await session.execute(
                    text(f"CREATE TABLE {name} PARTITION OF {table} FOR VALUES FROM ('{start}') TO ('{end}')")
                )
                created.append(name)
    await engine.dispose()
    return created


@job("maintenance.partitions")
async def partitions_job(_: dict[str, Any]) -> None:
    await ensure_partitions()
