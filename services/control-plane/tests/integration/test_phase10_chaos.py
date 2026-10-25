"""Phase 10 chaos tests: the failure-mode table (operations.md §3) on a live API.

* runner host dies mid-run → lease expires, run ``errored: runner lost``, quota refunded,
  never retried automatically;
* operator kill switch (the real CLI) → admission refuses new runs with Retry-After, active
  leases are told to cancel at their next heartbeat, the gauge shows it; off → runs resume;
* Redis down → reads keep working (rate limiter fails open), run creation fails closed;
* Postgres down → /metrics still answers, reporting ``re_metrics_db_up 0``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.keys import RunnerKeys

from reliability.modules.runners.service import reap_expired
from reliability.platform import metrics
from tests.conftest import OrgFixture, auth, make_org, sql
from tests.integration.test_phase3_e2e import _setup, live_api, runner_config  # noqa: F401
from tests.support.target_app import TargetApp

pytestmark = pytest.mark.integration


def _cli(*args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "reliability.cli", *args], capture_output=True, text=True, env=os.environ.copy()
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


async def _load_run(client: httpx.AsyncClient, org: OrgFixture, target_id: str, h: dict[str, str]) -> httpx.Response:
    profile = await client.post(
        f"/v1/projects/{org.project_id}/load-profiles",
        headers=h,
        json={
            "name": f"p-{uuid.uuid4().hex[:6]}",
            "type": "load",
            "max_vus": 2,
            "stages": [{"duration_s": 60, "target": 2}],
        },
    )
    assert profile.status_code == 201, profile.text
    return await client.post(
        f"/v1/projects/{org.project_id}/runs",
        headers={**h, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "kind": "load",
            "load_profile_id": profile.json()["id"],
            "environment": {"type": "target_url", "target_id": target_id},
        },
    )


async def test_runner_loss_and_kill_switch(live_api: str, tmp_path: object) -> None:  # noqa: F811
    org = make_org("chaos")
    async with httpx.AsyncClient(base_url=live_api, timeout=30) as client, TargetApp() as app:
        target, _suite, h = await _setup(client, org, app)
        sql(  # the target's first (approved) load test already happened
            "INSERT INTO runs (id, org_id, project_id, target_id, kind, environment_kind, status, requested_via, "
            "config_snapshot, quota_reservation) VALUES (%s,%s,%s,%s,'load','target_url','passed','ui','{}','{}')",
            uuid.uuid4(),
            org.org_id,
            org.project_id,
            uuid.UUID(target["id"]),
        )

        # --- Runner host dies mid-run ------------------------------------------------------
        created = await _load_run(client, org, target["id"], h)
        assert created.status_code == 202 and created.json()["status"] == "queued", created.text
        run_id = created.json()["id"]
        reserved = sql("SELECT sum(quantity) FROM usage_ledger WHERE org_id = %s AND kind = 'vu_minutes'", org.org_id)
        assert reserved[0][0] > 0
        cfg = runner_config(live_api, tmp_path)
        runner = LeaseClient(cfg)
        lease = await runner.acquire(RunnerKeys().public_b64, ["load"], {"slots_free": 1}, wait_seconds=5)
        assert lease and lease["job_type"] == "load"
        assert (await runner.heartbeat(lease["lease_id"], "running", None))["action"] == "continue"
        await runner.close()
        # …and then the host vanishes: no more heartbeats. Past the TTL the reaper expires it.
        sql(
            "UPDATE runner_leases SET expires_at = now() - interval '1 second' WHERE id = %s",
            uuid.UUID(lease["lease_id"]),
        )
        before = metrics.LEASES_EXPIRED.labels("load")._value.get()
        assert await reap_expired() == 1
        assert metrics.LEASES_EXPIRED.labels("load")._value.get() == before + 1
        lost = (await client.get(f"/v1/runs/{run_id}", headers=h)).json()
        assert lost["status"] == "errored" and lost["status_reason"] == "runner lost (missed heartbeats)", lost
        refunded = sql("SELECT sum(quantity) FROM usage_ledger WHERE org_id = %s AND kind = 'vu_minutes'", org.org_id)
        assert refunded[0][0] == 0, refunded  # reservation released, nothing consumed
        assert sql("SELECT count(*) FROM runs WHERE target_id = %s AND status = 'queued'", uuid.UUID(target["id"])) == [
            (0,)
        ]  # load runs are never retried automatically

        # --- Kill switch --------------------------------------------------------------------
        second = await _load_run(client, org, target["id"], h)
        assert second.json()["status"] == "queued"
        runner = LeaseClient(cfg)
        lease = await runner.acquire(RunnerKeys().public_b64, ["load"], {"slots_free": 1}, wait_seconds=5)
        assert lease
        out = _cli("kill-switch", "on")
        assert "cancel requested for 1 active lease(s)" in out and "kill switch on (global)" in out
        assert (await runner.heartbeat(lease["lease_id"], "running", None))["action"] == "cancel"
        await runner.close()
        refused = await _load_run(client, org, target["id"], h)
        assert refused.status_code == 503 and refused.headers["Retry-After"] == "600", refused.text
        assert "re_kill_switch_active 1.0" in (await client.get("/metrics")).text
        _cli("kill-switch", "off")
        sql("UPDATE runs SET status = 'cancelled' WHERE id = %s", uuid.UUID(second.json()["id"]))
        resumed = await _load_run(client, org, target["id"], h)
        assert resumed.status_code == 202, resumed.text


async def test_redis_down_fails_closed_for_runs_and_open_for_reads(
    api: httpx.AsyncClient, org_a: OrgFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reliability.platform import redis_client
    from reliability.platform.settings import get_settings

    await redis_client.close()
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")  # nothing listens there
    h = auth(org_a.email)
    try:
        read = await api.get(f"/v1/projects/{org_a.project_id}", headers=h)
        assert read.status_code == 200, read.text
        run = await api.post(
            f"/v1/projects/{org_a.project_id}/runs",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={"kind": "functional", "environment": {"type": "target_url", "target_id": str(uuid.uuid4())}},
        )
        assert run.status_code == 503 and run.headers["Retry-After"] == "5", run.text
        assert run.json()["error"]["code"] == "UNAVAILABLE"
        ready = await api.get("/readyz")
        assert ready.status_code == 503 and ready.json()["checks"] == {"postgres": True, "redis": False}
    finally:
        await redis_client.close()


async def test_metrics_endpoint_survives_a_database_outage(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from reliability.modules.ops import metrics as ops_metrics

    @asynccontextmanager
    async def down():  # type: ignore[no-untyped-def]
        raise ConnectionRefusedError("postgres is down")
        yield

    monkeypatch.setattr(ops_metrics, "system_tx", down)
    monkeypatch.setattr(ops_metrics, "CACHE_S", 0.0)
    body = (await api.get("/metrics")).text
    assert "re_metrics_db_up 0.0" in body and "re_http_requests_total" in body
