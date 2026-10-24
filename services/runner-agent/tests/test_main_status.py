"""The runner's local status file feeds the host agent (SlotUtilization metric, drain on
termination)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from runner_agent.config import Config
from runner_agent.main import serve


class _Client:
    def __init__(self, leases: list[dict[str, Any]]) -> None:
        self.leases = leases

    async def acquire(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        await asyncio.sleep(0.01)
        return self.leases.pop(0) if self.leases else None

    async def close(self) -> None:
        pass


async def test_status_tracks_busy_slots_and_draining(tmp_path: Path, monkeypatch: Any) -> None:
    gate = asyncio.Event()
    seen: list[dict[str, Any]] = []
    status = tmp_path / "status.json"

    async def fake_run(self: Any, lease: dict[str, Any]) -> None:
        seen.append(json.loads(status.read_text()))
        await gate.wait()

    monkeypatch.setattr("runner_agent.executor.JobRunner.run", fake_run)
    cfg = Config(env="test", api_url="http://x", runner_id="r", runner_token="t", workdir=str(tmp_path), slots=2)
    stop = asyncio.Event()
    task = asyncio.create_task(serve(cfg, stop, _Client([{"lease_id": "a"}, {"lease_id": "b"}]), max_jobs=2))  # type: ignore[arg-type]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(seen) == 2:
            break
    now = json.loads(status.read_text())
    assert now["slots"] == 2 and now["busy"] == 2
    stop.set()
    gate.set()
    await task
    final = json.loads(status.read_text())
    assert final["busy"] == 0 and final["draining"] is True
