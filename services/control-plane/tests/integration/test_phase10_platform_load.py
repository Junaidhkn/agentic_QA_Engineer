"""Phase 10: the platform load harness (NFR-4) against a live API with real runners and k6,
scaled down — 3 lanes kept busy with load runs plus Poisson functional arrivals for one
minute on 4 single-slot runners. The full replay (25 concurrent load runs + 100 functional
runs/hour) runs against staging with ``benchmarks/harness/platform_load.py``."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.main import serve

from reliability.modules.runners.auth import issue_runner_token
from reliability.platform.outbox import drain_in_process
from tests.conftest import make_org, sql
from tests.integration.test_phase3_e2e import _setup, live_api, runner_config  # noqa: F401
from tests.support.target_app import TargetApp

pytestmark = pytest.mark.integration

HARNESS = Path(__file__).resolve().parents[4] / "benchmarks" / "harness" / "platform_load.py"


def _harness():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("platform_load", HARNESS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["platform_load"] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


async def _drain_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await drain_in_process()
        await asyncio.sleep(0.5)


async def test_platform_load_harness_meets_job_start_slo_at_small_scale(
    live_api: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    harness = _harness()
    lanes = []
    async with contextlib.AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=live_api, timeout=30))
        for i in range(3):
            org = make_org(f"lane{i}")
            app = await stack.enter_async_context(TargetApp())
            target, _suite, h = await _setup(client, org, app)
            profile = await client.post(
                f"/v1/projects/{org.project_id}/load-profiles",
                headers=h,
                json={
                    "name": "lane",
                    "type": "load",
                    "max_vus": 3,
                    "stages": [{"duration_s": 10, "target": 3}],
                    "think_time_min_s": 0,
                    "think_time_max_s": 0.05,
                    "thresholds": {"p95_ms": 5000, "error_rate": 0.5},
                },
            )
            assert profile.status_code == 201, profile.text
            # The lane's target already had its approved first load test (as staging lanes must).
            sql(
                "INSERT INTO runs (id, org_id, project_id, target_id, kind, environment_kind, status, requested_via, "
                "config_snapshot, quota_reservation) VALUES (%s,%s,%s,%s,'load','target_url','passed','ui','{}','{}')",
                uuid.uuid4(),
                org.org_id,
                org.project_id,
                uuid.UUID(target["id"]),
            )
            lanes.append(harness.Lane(f"dev:{org.email}", str(org.project_id), target["id"], profile.json()["id"]))

        stop = asyncio.Event()
        workers = []
        for n in range(4):
            cfg = runner_config(live_api, tmp_path / f"r{n}").model_copy(
                update={
                    "runner_id": f"load-runner-{n}",
                    "runner_token": issue_runner_token(f"load-runner-{n}"),
                    "job_types": ["load", "functional"],
                }
            )
            workers.append(asyncio.create_task(serve(cfg, stop, LeaseClient(cfg))))
        drainer = asyncio.create_task(_drain_forever(stop))
        try:
            report = await harness.run(live_api, lanes, minutes=1.0, functional_per_hour=360, poll_s=1.0)
        finally:
            stop.set()
            await asyncio.gather(*workers, drainer, return_exceptions=True)

    md = harness.markdown(report)
    print(md)
    assert report["passed"], md + str(report)
    js = report["job_start_s"]
    assert js["load"]["n"] >= 3 and js["functional"]["n"] >= 2, md
    assert js["all"]["p95"] < 30
    assert not report["rejected_submissions"] and not report["platform_errors"]
    assert report["statuses"].get("passed", 0) >= js["all"]["n"] - 1, report["statuses"]
    assert "PASS" in md and "| load |" in md
