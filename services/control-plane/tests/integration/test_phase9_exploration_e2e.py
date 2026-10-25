"""Phase 9 (FR-8): discovery with browser exploration of a verified target, end to end — the
discovery job queues a ``ui_explore`` job, a real runner crawls the fixture app with Playwright
(test login from project secrets, action budget, denylist), and discovery resumes with the crawl:
pages and observed API calls become routes, submitted forms become reviewable browser
candidates. Destructive actions on the fixture are never requested. Skipped without
node + Playwright."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from runner_agent.client import LeaseClient
from runner_agent.main import serve

from reliability.platform.outbox import drain_in_process
from tests.conftest import auth, make_org, sql
from tests.integration.test_phase3_e2e import live_api, runner_config  # noqa: F401

PLAYWRIGHT_DIR = Path(__file__).resolve().parents[4] / "runners" / "images" / "playwright"
WELL_KNOWN = "/.well-known/reliability-engineer.txt"


def _playwright() -> bool:
    if not shutil.which("node"):
        return False
    probe = (
        "for (const m of ['@playwright/test','playwright']) { try { require(m); process.exit(0) } catch (e) {} }"
        " process.exit(1)"
    )
    return subprocess.run(["node", "-e", probe], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _playwright(), reason="node + Playwright not available"),
]

PAGES = {
    "/": """<h1>Notes</h1><a href="/notes">Notes</a> <a href="/login">Sign in</a>
      <a href="/account/close">Close account</a>""",
    "/login": """<h1>Sign in</h1><form method="post" action="/login">
      <label>Email <input type="email" name="email"></label>
      <label>Password <input type="password" name="password"></label><button>Sign in</button></form>""",
    "/notes": """<h1>Your notes</h1>
      <form method="post" action="/notes"><label>Title <input name="title" required></label>
        <button type="submit">Create note</button></form>
      <form method="post" action="/notes/1/delete"><button type="submit">Delete</button></form>
      <script>fetch('/api/notes')</script>""",
    "/notes/created": "<h1>Note saved</h1><a href='/notes'>Back</a>",
}


class _App:
    def __init__(self) -> None:
        self.token = ""
        self.hits: list[tuple[str, str, str]] = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, body: str, ctype: str = "text/html") -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                app.hits.append(("GET", path, ""))
                if path == WELL_KNOWN:
                    self._send(200, app.token, "text/plain")
                elif path == "/api/notes":
                    self._send(200, '[{"id": 1}]', "application/json")
                elif path in PAGES:
                    self._send(200, f"<!doctype html><title>Notes</title><body>{PAGES[path]}</body>")
                else:
                    self._send(404, "<h1>Not found</h1>")

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                app.hits.append(("POST", path, body))
                self.send_response(303)
                self.send_header("Location", "/notes" if path == "/login" else "/notes/created")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def notes_app() -> Iterator[_App]:
    app = _App()
    yield app
    app.server.shutdown()


async def _discovery(client: httpx.AsyncClient, disc_id: str, h: dict[str, str], wanted: set[str]) -> dict:
    deadline = time.monotonic() + 60
    run: dict = {}
    while time.monotonic() < deadline:
        await drain_in_process()
        run = (await client.get(f"/v1/discoveries/{disc_id}", headers=h)).json()
        if run["status"] in wanted:
            return run
        await asyncio.sleep(0.3)
    raise AssertionError(f"discovery did not reach {wanted}: {run.get('status')}")


async def test_discovery_explores_a_verified_target_safely(
    live_api: str,  # noqa: F811
    notes_app: _App,
    tmp_path: Path,
) -> None:
    org = make_org("explore")
    h = auth(org.email)
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        target = (
            await client.post(
                f"/v1/projects/{org.project_id}/targets",
                headers=h,
                json={"base_url": notes_app.url, "environment": "staging"},
            )
        ).json()
        body = {"browser_exploration": {"target_id": target["id"], "action_budget": 40, "time_budget_s": 120}}
        # Exploration needs a verified target.
        early = await client.post(
            f"/v1/projects/{org.project_id}/discoveries", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=body
        )
        assert early.status_code == 409 and early.json()["error"]["code"] == "TARGET_NOT_VERIFIED", early.text
        notes_app.token = target["verification"]["http_file"]["content"]
        assert (await client.post(f"/v1/targets/{target['id']}:verify", headers=h)).json()["status"] == "verified"

        for name, value in (("QA_USER", "qa@example.com"), ("QA_PASSWORD", "pw-Secret-981")):
            put = await client.put(f"/v1/projects/{org.project_id}/secrets/{name}", headers=h, json={"value": value})
            assert put.status_code == 204, put.text
        missing = {"browser_exploration": {**body["browser_exploration"], "password_secret": "NOPE"}}
        bad = await client.post(
            f"/v1/projects/{org.project_id}/discoveries",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json=missing,
        )
        assert bad.status_code == 404, bad.text

        body["browser_exploration"] |= {"username_secret": "QA_USER", "password_secret": "QA_PASSWORD"}
        started = await client.post(
            f"/v1/projects/{org.project_id}/discoveries", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=body
        )
        assert started.status_code == 202, started.text
        disc_id = started.json()["id"]
        await drain_in_process()
        queued = (await client.get(f"/v1/discoveries/{disc_id}", headers=h)).json()
        assert queued["status"] == "running" and any(p["step"] == "explore_ui" for p in queued["progress"]), queued
        jobs = sql("SELECT job_type, status FROM execution_jobs WHERE subject_id = %s", uuid.UUID(disc_id))
        assert jobs == [("ui_explore", "queued")], jobs

        cfg = runner_config(live_api, tmp_path).model_copy(
            update={"job_types": ["ui_explore"], "playwright_dir": str(PLAYWRIGHT_DIR)}
        )
        assert await serve(cfg, asyncio.Event(), LeaseClient(cfg), max_jobs=1) == 1
        run = await _discovery(client, disc_id, h, {"awaiting_review", "completed", "failed"})
        assert run["status"] in ("awaiting_review", "completed"), run

    hit = {(m, p) for m, p, _ in notes_app.hits}
    assert ("POST", "/notes/1/delete") not in hit and ("GET", "/account/close") not in hit
    assert ("POST", "/login") in hit and ("POST", "/notes") in hit
    login = next(b for m, p, b in notes_app.hits if (m, p) == ("POST", "/login"))
    assert "qa%40example.com" in login

    # The crawl's routes and candidates landed in discovery; the password never did.
    steps = {p["step"]: p for p in run["progress"]}
    explored = steps["explore_ui"]
    assert explored["skipped_by_reason"].get("denylisted action") and not explored["error"], explored
    assert run["stats"]["routes"]["pages"] >= 3 and run["stats"]["routes"]["api"] >= 2, run["stats"]
    specs = [
        s[0]
        for s in sql(
            "SELECT spec FROM workflows WHERE project_id = %s AND status = 'candidate' AND kind = 'browser'",
            org.project_id,
        )
    ]
    create = next(ir for ir in specs if ir["source"]["type"] == "browser_exploration" and "create-note" in ir["key"])
    assert create["preconditions"][0]["credential_ref"] == "secret:QA_PASSWORD"
    assert create["steps"][0] == {"id": "open", "action": "navigate", "url": "/notes"}
    assert {"type": "text_visible", "value": "Note saved"} in [s.get("assertion") for s in create["steps"]]
    job_result = sql("SELECT result FROM execution_jobs WHERE subject_id = %s", uuid.UUID(disc_id))
    assert "pw-Secret-981" not in str(job_result) + str(specs) + str(run)
