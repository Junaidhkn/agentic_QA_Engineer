"""End-to-end: API server (subprocess) → runner agent (process driver) → real k6 → analysis.

Covers UF-3/UF-4: verified target, manual workflow, suite compilation, load profile,
approval of the first load run, lease protocol, live progress, artifacts, verdicts,
concurrency probe detecting a race (FR-13) and stop-within-10-seconds (FR-18).
Skipped when no k6 binary is available (CI installs it).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.config import Config
from runner_agent.main import serve

from reliability.platform.outbox import drain_in_process
from tests.conftest import OrgFixture, auth, make_org, sql
from tests.support.target_app import TargetApp

pytestmark = pytest.mark.integration

K6 = (
    os.environ.get("RE_TEST_K6_BINARY")
    or shutil.which("k6")
    or ("/tmp/claude-0/-home-user-agentic-QA-Engineer/e32a83d2-99fd-5d7a-9623-151a50310f66/scratchpad/bin/k6")
)
if not os.path.exists(K6):
    pytest.skip("k6 binary not available", allow_module_level=True)

IR = {
    "ir_version": "1.0",
    "key": "checkout.place_order",
    "title": "Place an order",
    "kind": "api",
    "criticality": "critical",
    "preconditions": [
        {
            "type": "login",
            "api": {
                "id": "login",
                "request": {"method": "POST", "path": "/api/login", "json": {"user": "x"}},
                "extract": {"token": "$.token"},
            },
        }
    ],
    "data": {"vars": {"qty": {"range": [1, 3]}}},
    "steps": [
        {
            "id": "cart",
            "request": {"method": "POST", "path": "/api/cart", "json": {"qty": "{{qty}}"}},
            "extract": {"cart_id": "$.id"},
            "expect": {"status": [201]},
        },
        {
            "id": "order",
            "request": {"method": "POST", "path": "/api/orders", "json": {"cart": "{{cart_id}}"}},
            "expect": {"status": [201]},
        },
    ],
    "invariants": [
        {
            "id": "stock-non-negative",
            "type": "concurrency_probe",
            "description": "stock never < 0",
            "parallel": 10,
            "step_ref": "order",
            "check": {
                "request": {"method": "GET", "path": "/api/products/1"},
                "jsonpath": "$.stock",
                "op": ">=",
                "value": 0,
            },
        }
    ],
    "load": {"weight": 1, "think_time_s": [0, 0.05]},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_api() -> Iterator[str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "RE_API_BASE_URL": base,
        "RE_ALLOW_PRIVATE_TARGETS": "true",
        "RE_RUNNER_LEASE_TTL_SECONDS": "15",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "reliability.main_api:app", "--port", str(port), "--log-level", "warning"],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    )
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(10)


def runner_config(base: str, tmp_path: object) -> Config:
    from reliability.modules.runners.auth import issue_runner_token

    return Config(
        env="test",
        api_url=base,
        runner_id="test-runner",
        runner_token=issue_runner_token("test-runner"),
        driver="process",
        k6_binary=K6,
        workdir=str(tmp_path),
        slots=1,
        job_types=["load", "probe", "functional"],
    )


async def _wait_status(
    client: httpx.AsyncClient,
    run_id: str,
    headers: dict[str, str],
    statuses: set[str],
    timeout: float = 60,  # noqa: ASYNC109
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        if run["status"] in statuses:
            return run
        await drain_in_process()
        await asyncio.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}; last: {run['status']}")


async def _setup(client: httpx.AsyncClient, org: OrgFixture, target_app: TargetApp) -> tuple[dict, str, dict]:
    h = auth(org.email)
    target = (
        await client.post(
            f"/v1/projects/{org.project_id}/targets",
            headers=h,
            json={"base_url": target_app.url, "environment": "staging"},
        )
    ).json()
    target_app.token = target["verification"]["http_file"]["content"]  # type: ignore[attr-defined]
    verified = (await client.post(f"/v1/targets/{target['id']}:verify", headers=h)).json()
    assert verified["status"] == "verified", verified
    wf = await client.post(f"/v1/projects/{org.project_id}/workflows", headers=h, json={"spec": IR})
    assert wf.status_code == 201, wf.text
    suite = await client.post(
        f"/v1/projects/{org.project_id}/test-suites", json={}, headers={**h, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert suite.status_code == 201, suite.text
    assert {c["type"] for c in suite.json()["cases"]} == {"k6_scenario", "k6_probe"}
    return verified, suite.json()["id"], h


async def test_load_run_end_to_end_with_approval_probe_and_cancel(live_api: str, tmp_path: object) -> None:
    org = make_org("e2e")
    async with httpx.AsyncClient(base_url=live_api, timeout=30) as client, TargetApp() as target_app:
        target, _suite_id, h = await _setup(client, org, target_app)

        profile = await client.post(
            f"/v1/projects/{org.project_id}/load-profiles",
            headers=h,
            json={
                "name": "tiny-load",
                "type": "load",
                "max_vus": 5,
                "stages": [{"duration_s": 10, "target": 5}],
                "think_time_min_s": 0,
                "think_time_max_s": 0.05,
                "thresholds": {"p95_ms": 2000, "error_rate": 0.05},
            },
        )
        assert profile.status_code == 201, profile.text

        body = {
            "kind": "load",
            "load_profile_id": profile.json()["id"],
            "environment": {"type": "target_url", "target_id": target["id"]},
        }
        key = str(uuid.uuid4())
        created = await client.post(
            f"/v1/projects/{org.project_id}/runs", json=body, headers={**h, "Idempotency-Key": key}
        )
        assert created.status_code == 202, created.text
        run = created.json()
        assert run["status"] == "pending_approval"  # first load test against this target
        replay = await client.post(
            f"/v1/projects/{org.project_id}/runs", json=body, headers={**h, "Idempotency-Key": key}
        )
        assert replay.json()["id"] == run["id"] and replay.headers.get("Idempotent-Replayed") == "true"

        approvals = (await client.get("/v1/approvals", headers=h)).json()
        assert approvals[0]["summary"]["reasons"] == ["first load test against this target"]
        decided = await client.post(
            f"/v1/approvals/{approvals[0]['id']}:decide",
            headers=h,
            json={"decision": "approved", "reason": "pre-launch"},
        )
        assert decided.status_code == 200
        assert (await client.get(f"/v1/runs/{run['id']}", headers=h)).json()["status"] == "queued"

        cfg = runner_config(live_api, tmp_path)
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        done = await _wait_status(client, run["id"], h, {"passed", "failed", "invalid", "errored"})
        assert done["status"] == "passed", done
        assert done["summary"]["overall"]["requests"] > 50
        assert done["summary"]["generator"]["valid"] is True
        scopes = {s["scope"] for s in (await client.get(f"/v1/runs/{run['id']}/summaries", headers=h)).json()}
        assert {"overall", "scenario:checkout.place_order", "endpoint:POST /api/orders"} <= scopes
        kinds = {a["kind"] for a in (await client.get(f"/v1/runs/{run['id']}/artifacts", headers=h)).json()}
        assert {"k6_summary", "k6_timeseries"} <= kinds
        progress = [
            e
            for e in (await client.get(f"/v1/runs/{run['id']}/events-log", headers=h)).json()
            if e["type"] == "progress"
        ]
        assert progress and progress[-1]["payload"]["rps"] >= 0
        used = sql("SELECT sum(quantity) FROM usage_ledger WHERE org_id = %s AND kind = 'vu_minutes'", org.org_id)[0][0]
        assert 0 < used <= 5  # reservation (5) replaced by actual usage

        # Concurrency probe finds the read-modify-write race (FR-13).
        probe = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": "probe-1-xx"},
            json={"kind": "probe", "environment": {"type": "target_url", "target_id": target["id"]}},
        )
        assert probe.json()["status"] == "queued", probe.text
        await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1)
        probe_done = await _wait_status(client, probe.json()["id"], h, {"passed", "failed", "errored"})
        assert probe_done["status"] == "failed" and "invariant violated" in probe_done["status_reason"]
        await drain_in_process()
        findings = (await client.get(f"/v1/projects/{org.project_id}/findings?status=active", headers=h)).json()
        assert isinstance(findings, list), findings
        race = next(f for f in findings if f["type"] == "invariant_violation")
        assert race["severity"] == "critical" and race["fingerprint"].startswith("invariant:checkout.place_order/")
        detail = (await client.get(f"/v1/findings/{race['id']}", headers=h)).json()
        assert detail["evidence"] and detail["evidence"][0]["kind"] == "experiment"

        # The finished load run has steady-state summaries, chart series and a replayable SSE log.
        metrics = (await client.get(f"/v1/runs/{run['id']}/metrics", headers=h)).json()
        assert metrics["points"] and "endpoint:POST /api/orders" in metrics["scopes"]
        async with client.stream("GET", f"/v1/runs/{run['id']}/events", headers={**h, "Last-Event-ID": "1"}) as resp:
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: end" in body:
                    break
        assert "event: progress" in body and "id: 1\n" not in body and "event: end" in body
        summary = (await client.get(f"/v1/projects/{org.project_id}/summary", headers=h)).json()
        assert summary["latest_runs"]["load"]["status"] == "passed"
        assert summary["open_findings"].get("critical") == 1

        # Stop halts traffic within 10 s (FR-18).
        long_profile = (
            await client.post(
                f"/v1/projects/{org.project_id}/load-profiles",
                headers=h,
                json={
                    "name": "long",
                    "type": "load",
                    "max_vus": 3,
                    "stages": [{"duration_s": 120, "target": 3}],
                    "think_time_min_s": 0,
                    "think_time_max_s": 0.05,
                },
            )
        ).json()
        long_run = (
            await client.post(
                f"/v1/projects/{org.project_id}/runs",
                headers={**h, "Idempotency-Key": "long-1-xx"},
                json={
                    "kind": "load",
                    "load_profile_id": long_profile["id"],
                    "environment": {"type": "target_url", "target_id": target["id"]},
                },
            )
        ).json()
        assert long_run["status"] == "queued"
        runner_task = asyncio.create_task(serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1))
        await _wait_status(client, long_run["id"], h, {"running"}, timeout=30)
        await asyncio.sleep(3)
        stop_at = time.monotonic()
        cancel = await client.post(f"/v1/runs/{long_run['id']}:cancel", headers=h, json={"reason": "test stop"})
        assert cancel.status_code == 202
        await asyncio.wait_for(runner_task, timeout=30)
        assert time.monotonic() - stop_at <= 10, "traffic must stop within 10 seconds"
        stopped = await _wait_status(client, long_run["id"], h, {"cancelled"}, timeout=20)
        assert stopped["status_reason"] == "test stop"
        (health,) = sql("SELECT generator_health FROM runs WHERE id = %s", uuid.UUID(long_run["id"]))[0]
        assert 0 <= health["kill_latency_s"] <= 10, health

        # Platform metrics saw the job starts and the kill (operations §6).
        scraped = (await client.get("/metrics")).text
        assert 're_kill_latency_seconds_count{job_type="load"} 1.0' in scraped
        assert 're_job_start_seconds_count{job_type="load"}' in scraped
        assert 're_job_start_seconds_count{job_type="probe"}' in scraped
        assert str(org.project_id) not in scraped and long_run["id"] not in scraped


async def test_unverified_or_production_targets_are_refused(live_api: str) -> None:
    org = make_org("policy")
    async with httpx.AsyncClient(base_url=live_api, timeout=30) as client, TargetApp() as target_app:
        target, _suite_id, h = await _setup(client, org, target_app)
        sql("UPDATE targets SET environment = 'production' WHERE id = %s", target["id"])
        profile = (
            await client.post(
                f"/v1/projects/{org.project_id}/load-profiles",
                headers=h,
                json={"name": "p", "type": "load", "max_vus": 5, "stages": [{"duration_s": 10, "target": 5}]},
            )
        ).json()
        refused = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": "prod-1-xx"},
            json={
                "kind": "load",
                "load_profile_id": profile["id"],
                "environment": {"type": "target_url", "target_id": target["id"]},
            },
        )
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "ENVIRONMENT_POLICY_VIOLATION"
        sql("UPDATE targets SET environment = 'staging', status = 'revoked' WHERE id = %s", target["id"])
        refused = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": "rev-1-xxx"},
            json={
                "kind": "load",
                "load_profile_id": profile["id"],
                "environment": {"type": "target_url", "target_id": target["id"]},
            },
        )
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "TARGET_NOT_VERIFIED"
