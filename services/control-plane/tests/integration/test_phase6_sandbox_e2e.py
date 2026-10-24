"""Phase 6 end to end: the customer app is built and run in a sandbox from source.

Reference app ``fastapi-tasks`` (FastAPI + SQLAlchemy + Alembic + Postgres) uploaded as an
archive → standalone sandbox build (plan → build → ephemeral Postgres → migrate → seed →
health) → load run with ``environment.type = sandbox_build`` (runner builds and starts the app,
k6 tests it, DB statistics and the sandbox report are attached) → an unbuildable source makes
the run ``invalid``. Uses the process sandbox (Docker is not required); skipped without k6.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.main import serve

from reliability.modules.runners import service as runners
from reliability.platform.outbox import drain_in_process
from tests.conftest import SUPERUSER_URL, auth, make_org, sql
from tests.integration.test_phase3_e2e import _wait_status, live_api, runner_config  # noqa: F401

pytestmark = pytest.mark.integration

TASKS = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps" / "fastapi-tasks"

IR = {
    "ir_version": "1.0",
    "key": "tasks.create-and-find",
    "title": "Create a task and find it by assignee",
    "kind": "api",
    "criticality": "high",
    "data": {"vars": {"who": {"faker": "internet.email"}}},
    "steps": [
        {"id": "projects", "request": {"method": "GET", "path": "/api/projects"}, "extract": {"project_id": "$[0].id"}},
        {
            "id": "create",
            "request": {
                "method": "POST",
                "path": "/api/tasks",
                "json": {"project_id": "{{ project_id }}", "title": "Load test", "assignee_email": "{{ who }}"},
            },
            "extract": {"task_id": "$.id"},
            "expect": {"status": [201]},
        },
        {"id": "find", "request": {"method": "GET", "path": "/api/tasks", "query": {"assignee": "user7@example.com"}}},
        {"id": "list", "request": {"method": "GET", "path": "/api/projects/{{ project_id }}/tasks"}},
    ],
    "load": {"weight": 1, "think_time_s": [0, 0.05]},
}


def _archive(root: Path | None, files: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if root is not None:
            for p in root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    tf.add(p, arcname=f"tasks-main/{p.relative_to(root)}")
        for name, text in (files or {}).items():
            data = text.encode()
            info = tarfile.TarInfo(f"x/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sandbox_runner(base: str, tmp_path: Path, job_types: list[str]):  # type: ignore[no-untyped-def]
    return runner_config(base, tmp_path).model_copy(
        update={
            "job_types": job_types,
            "sandbox_postgres_url": SUPERUSER_URL,
            "sandbox_inherit_site_packages": True,
        }
    )


async def _upload(client: httpx.AsyncClient, project_id: uuid.UUID, h: dict[str, str], data: bytes) -> str:
    up = await client.post(
        f"/v1/projects/{project_id}/source-archives",
        content=data,
        headers={**h, "Content-Type": "application/octet-stream"},
    )
    assert up.status_code == 201, up.text
    return str(up.json()["archive_artifact_id"])


async def test_sandbox_build_and_load_run_from_source(live_api: str, tmp_path: Path) -> None:  # noqa: F811
    org = make_org("sbx")
    h = auth(org.email)
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        archive = await _upload(client, org.project_id, h, _archive(TASKS))

        # 1. Standalone buildability check.
        build = await client.post(
            f"/v1/projects/{org.project_id}/sandbox-builds", headers=h, json={"archive_artifact_id": archive}
        )
        assert build.status_code == 202, build.text
        await drain_in_process()
        planned = (await client.get(f"/v1/sandbox-builds/{build.json()['id']}", headers=h)).json()
        assert planned["status"] == "building", planned
        assert planned["plan"]["services"]["postgres"] and planned["plan"]["migrate"]
        cfg = _sandbox_runner(live_api, tmp_path, ["sandbox_build"])
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        done = (await client.get(f"/v1/sandbox-builds/{build.json()['id']}", headers=h)).json()
        assert done["status"] == "succeeded", (done["error"], done["report"])
        report = done["report"]
        assert report["healthy"] and report["migrations"] == "applied" and report["seed"] == "applied"
        tables = {t["relname"]: t for t in report["db"]["tables"]}
        assert tables["tasks"]["n_live_tup"] == 1_000_000
        log = await client.get(f"/v1/sandbox-builds/{build.json()['id']}/log", headers=h)
        assert log.status_code == 200 and "alembic" in (await client.get(log.json()["url"])).text

        # 2. Load run against a sandbox built from the same source.
        wf = await client.post(f"/v1/projects/{org.project_id}/workflows", headers=h, json={"spec": IR})
        assert wf.status_code == 201, wf.text
        suite = await client.post(
            f"/v1/projects/{org.project_id}/test-suites", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json={}
        )
        profile = await client.post(
            f"/v1/projects/{org.project_id}/load-profiles",
            headers=h,
            json={
                "name": "sandbox-load",
                "type": "load",
                "max_vus": 5,
                "stages": [{"duration_s": 10, "target": 5}],
                "think_time_min_s": 0,
                "think_time_max_s": 0.05,
                "thresholds": {"p95_ms": 3000, "error_rate": 0.05},
            },
        )
        created = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "kind": "load",
                "suite_id": suite.json()["id"],
                "load_profile_id": profile.json()["id"],
                "environment": {"type": "sandbox_build", "archive_artifact_id": archive},
            },
        )
        assert created.status_code == 202, created.text
        run = created.json()
        assert run["status"] == "queued" and run["environment_kind"] == "sandbox_build"  # no approval: own sandbox
        # Not leasable before the plan is ready, nor by runners without sandbox capability.
        assert await runners.acquire("probe-runner", "unused", ["load", "sandbox_env"], wait_seconds=0) is None
        await drain_in_process()  # environments.prepare_run
        assert await runners.acquire("probe-runner", "unused", ["load"], wait_seconds=0) is None
        cfg = _sandbox_runner(live_api, tmp_path, ["load", "sandbox_env"])
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        finished = await _wait_status(client, run["id"], h, {"passed", "failed", "invalid", "errored"})
        assert finished["status"] == "passed", finished
        assert finished["summary"]["overall"]["requests"] > 20
        kinds = {a["kind"] for a in (await client.get(f"/v1/runs/{run['id']}/artifacts", headers=h)).json()}
        assert {"telemetry_snapshot", "log_bundle", "k6_summary"} <= kinds
        ref = sql("SELECT build_ref FROM runs WHERE id = %s", run["id"])[0][0]
        assert ref["status"] == "ready" and ref["plan"]["strategy"] == "template"
        sandbox = ref["report"]
        assert sandbox["healthy"] and sandbox["driver"] == "process"
        tasks = next(t for t in sandbox["db"]["tables"] if t["relname"] == "tasks")
        assert tasks["n_tup_ins"] > 1_000_000  # seed + tasks created by the load test
        assert sandbox["db_samples"], "connection samples are collected during the run"
        used = sql(
            "SELECT coalesce(sum(quantity), 0) FROM usage_ledger WHERE org_id = %s AND kind = 'sandbox_minutes'",
            org.org_id,
        )[0][0]
        assert 0 < used <= 5  # reservation replaced by actual sandbox minutes

        # 3. Source the planner cannot build → the run is invalid, quota released.
        bad = await _upload(client, org.project_id, h, _archive(None, {"README.md": "# docs only"}))
        rejected = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "kind": "functional",
                "suite_id": suite.json()["id"],
                "environment": {"type": "sandbox_build", "archive_artifact_id": bad},
            },
        )
        assert rejected.status_code == 202, rejected.text
        await drain_in_process()
        deadline = time.monotonic() + 10
        status = {}
        while time.monotonic() < deadline:
            status = (await client.get(f"/v1/runs/{rejected.json()['id']}", headers=h)).json()
            if status["status"] == "invalid":
                break
            await asyncio.sleep(0.2)
        assert status["status"] == "invalid" and "no supported application" in status["status_reason"]
