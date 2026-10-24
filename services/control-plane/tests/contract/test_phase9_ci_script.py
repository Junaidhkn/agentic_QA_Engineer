"""The CI example script (examples/ci/reliability-check.sh) maps verdicts to exit codes:
0 passed, 1 failed, 2 not runnable, 3 waiting for approval — exercised against a stub API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "examples" / "ci" / "reliability-check.sh"
pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="jq not installed")


def _serve(
    verdicts: list[dict[str, Any]], create_status: int = 202
) -> tuple[ThreadingHTTPServer, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {
                    "path": self.path,
                    "body": body,
                    "idem": self.headers.get("Idempotency-Key"),
                    "auth": self.headers.get("Authorization"),
                }
            )
            if create_status != 202:
                self._send(create_status, {"error": {"code": "TARGET_NOT_VERIFIED", "message": "target not verified"}})
            else:
                self._send(202, {"id": "r1", "status": "queued"})

        def do_GET(self) -> None:
            self._send(200, verdicts.pop(0) if len(verdicts) > 1 else verdicts[0])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _verdict(**kw: Any) -> dict[str, Any]:
    base = {
        "run_id": "r1",
        "status": "running",
        "finished": False,
        "passed": None,
        "reason": None,
        "failed_thresholds": [],
        "summary": None,
        "new_findings": [],
        "url": "https://app/projects/p/runs/r1",
    }
    return {**base, **kw}


def _run(server: ThreadingHTTPServer, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    summary = tmp_path / "summary.md"
    full = {
        "PATH": os.environ["PATH"],
        "RE_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "RE_API_KEY": "rek_test",
        "RE_PROJECT_ID": "p",
        "RE_RUN_KIND": "load",
        "RE_TARGET_ID": "t1",
        "RE_LOAD_PROFILE_ID": "lp1",
        "RE_POLL_S": "0",
        "GITHUB_RUN_ID": "99",
        "GITHUB_STEP_SUMMARY": str(summary),
        **env,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=30)


def test_failed_run_fails_the_job(tmp_path: Path) -> None:
    failed = _verdict(
        status="failed",
        finished=True,
        passed=False,
        reason="thresholds failed",
        failed_thresholds=[{"metric": "p95_ms", "scope": "overall", "observed": 812, "threshold": 500}],
        summary={"p95_ms": 812, "error_rate": 0.002, "rps": 140},
        new_findings=[{"id": "f", "type": "slo_breach", "severity": "high", "title": "p95 812 ms"}],
    )
    server, seen = _serve([_verdict(), _verdict(), failed])
    out = _run(server, tmp_path)
    assert out.returncode == 1, out.stderr
    assert "threshold failed: overall p95_ms 812 > 500" in out.stdout and "new finding [high]" in out.stdout
    assert seen[0]["path"] == "/v1/projects/p/runs" and seen[0]["auth"] == "Bearer rek_test"
    assert seen[0]["body"] == {
        "kind": "load",
        "environment": {"type": "target_url", "target_id": "t1"},
        "load_profile_id": "lp1",
    }
    assert seen[0]["idem"] == "ci-99-1-load"
    assert "Reliability check: failed" in (tmp_path / "summary.md").read_text()


def test_passed_and_other_outcomes(tmp_path: Path) -> None:
    server, _ = _serve([_verdict(status="passed", finished=True, passed=True)])
    assert _run(server, tmp_path).returncode == 0
    server, _ = _serve([_verdict(status="errored", finished=True, passed=False, reason="runner lost")])
    assert _run(server, tmp_path).returncode == 2
    server, _ = _serve([_verdict(status="pending_approval")])
    assert _run(server, tmp_path).returncode == 3
    server, _ = _serve([_verdict()], create_status=409)
    bad = _run(server, tmp_path)
    assert bad.returncode == 2 and "target not verified" in bad.stderr
    server, _ = _serve([_verdict()])
    assert _run(server, tmp_path, RE_TIMEOUT_S="0").returncode == 2
