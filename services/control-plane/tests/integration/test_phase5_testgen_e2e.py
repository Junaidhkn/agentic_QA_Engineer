"""End-to-end UF-2 → UF-3 on the reference shop (FastAPI): source archive → discovery →
approve candidates → suite generation with dry-run validation on a runner (real k6, 3 repeats)
→ functional run of the validated suite. Gate: generated-test validity ≥ 85% (evaluation §4).
Skipped when no k6 binary is available (CI installs it)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.main import serve

from reliability.platform.outbox import drain_in_process
from tests.conftest import auth, make_org
from tests.integration.test_phase3_e2e import _free_port, _wait_status, live_api, runner_config  # noqa: F401
from tests.integration.test_phase5_discovery import SHOP, shop_archive

pytestmark = pytest.mark.integration


@pytest.fixture
def shop(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    port = _free_port()
    token_file = tmp_path / "verification.txt"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=SHOP,
        env={**os.environ, "VERIFICATION_TOKEN_FILE": str(token_file), "DEFECTS": ""},
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base, token_file
    proc.terminate()
    proc.wait(10)


async def _suite_status(client: httpx.AsyncClient, suite_id: str, h: dict[str, str], wanted: set[str]) -> dict:
    deadline = time.monotonic() + 60
    suite: dict = {}
    while time.monotonic() < deadline:
        await drain_in_process()
        suite = (await client.get(f"/v1/test-suites/{suite_id}", headers=h)).json()
        if suite["status"] in wanted:
            return suite
        await asyncio.sleep(0.3)
    raise AssertionError(f"suite did not reach {wanted}: {suite.get('status')}")


async def test_discover_generate_validate_and_run_reference_shop(
    live_api: str,  # noqa: F811
    shop: tuple[str, Path],
    tmp_path: Path,
) -> None:
    base, token_file = shop
    org = make_org("shop")
    h = auth(org.email)
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        target = (
            await client.post(
                f"/v1/projects/{org.project_id}/targets", headers=h, json={"base_url": base, "environment": "staging"}
            )
        ).json()
        token_file.write_text(target["verification"]["http_file"]["content"])
        assert (await client.post(f"/v1/targets/{target['id']}:verify", headers=h)).json()["status"] == "verified"

        up = await client.post(
            f"/v1/projects/{org.project_id}/source-archives",
            content=shop_archive(),
            headers={**h, "Content-Type": "application/octet-stream"},
        )
        disc = await client.post(
            f"/v1/projects/{org.project_id}/discoveries",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={"archive_artifact_id": up.json()["archive_artifact_id"]},
        )
        await drain_in_process()
        run = (await client.get(f"/v1/discoveries/{disc.json()['id']}", headers=h)).json()
        assert run["status"] == "awaiting_review", run

        candidates = (await client.get(f"/v1/projects/{org.project_id}/workflows?status=candidate", headers=h)).json()
        api_wfs = [w for w in candidates if w["kind"] == "api"]
        review = await client.post(
            f"/v1/projects/{org.project_id}/workflows:bulk-review",
            headers=h,
            json={"approve": [w["id"] for w in api_wfs], "reject": []},
        )
        assert review.status_code == 200

        created = await client.post(
            f"/v1/projects/{org.project_id}/test-suites",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={"validate_target_id": target["id"]},
        )
        assert created.status_code == 202 and created.json()["status"] == "generating", created.text
        suite_id = created.json()["id"]
        suite = await _suite_status(client, suite_id, h, {"validating"})

        cfg = runner_config(live_api, tmp_path).model_copy(update={"job_types": ["test_validation", "functional"]})
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        suite = await _suite_status(client, suite_id, h, {"ready", "partially_ready", "failed"})

        k6_cases = [c for c in suite["cases"] if c["type"] == "k6_scenario"]
        valid = [c for c in k6_cases if c["validation_status"] == "valid"]
        problems = {c["workflow_key"]: c["validation_detail"] for c in k6_cases if c not in valid}
        assert len(k6_cases) == len(api_wfs)
        assert len(valid) / len(k6_cases) >= 0.85, problems  # evaluation §4 gate
        assert suite["status"] == "ready", problems
        assert all(c["validation_detail"]["runs"] == 3 for c in valid)

        # The validated suite runs as a functional test against the target.
        func = await client.post(
            f"/v1/projects/{org.project_id}/runs",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "kind": "functional",
                "suite_id": suite_id,
                "environment": {"type": "target_url", "target_id": target["id"]},
            },
        )
        assert func.status_code == 202, func.text
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        done = await _wait_status(client, func.json()["id"], h, {"passed", "failed", "errored", "invalid"})
        assert done["status"] == "passed", done

        cov = (await client.get(f"/v1/projects/{org.project_id}/coverage", headers=h)).json()
        assert cov["totals"]["tested"] == len(api_wfs) and cov["totals"]["passing"] == len(api_wfs)
