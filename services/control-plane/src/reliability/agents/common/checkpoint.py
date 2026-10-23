"""LangGraph checkpointer wiring (ADR-0006).

Production and tests use ``AsyncPostgresSaver`` in the ``agent_state`` schema, reached with
the system role (threads are mapped to tenants in ``agent_threads``, which is under RLS).
``memory`` mode exists for pure unit tests of graph logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from reliability.platform.settings import get_settings

_memory = InMemorySaver()
_setup_done = False
_mode = "postgres"


def use_memory_checkpointer(enabled: bool = True) -> None:
    global _mode
    _mode = "memory" if enabled else "postgres"


def _dsn() -> str:
    return get_settings().database_system_url.replace("postgresql+psycopg://", "postgresql://", 1)


@asynccontextmanager
async def checkpointer() -> AsyncIterator[Any]:
    global _setup_done
    if _mode == "memory":
        yield _memory
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    conn = await AsyncConnection.connect(
        _dsn(), autocommit=True, prepare_threshold=0, row_factory=dict_row, options="-c search_path=agent_state"
    )
    try:
        saver = AsyncPostgresSaver(conn)
        if not _setup_done:
            await saver.setup()
            _setup_done = True
        yield saver
    finally:
        await conn.close()
