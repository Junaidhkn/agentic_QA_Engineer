"""Browser exploration (FR-8): the crawler stays on the target, respects its action budget and
never triggers denylisted actions — deletes, payments, account closure, email sends, logout —
on a fixture app full of them. Skipped without node + Playwright."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

EXPLORER = Path(__file__).resolve().parents[3] / "runners" / "images" / "playwright" / "explore.cjs"


def _playwright() -> bool:
    if not shutil.which("node"):
        return False
    probe = (
        "for (const m of ['@playwright/test','playwright']) { try { require(m); process.exit(0) } catch (e) {} }"
        " process.exit(1)"
    )
    return subprocess.run(["node", "-e", probe], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _playwright(), reason="node + Playwright not available")

PAGES = {
    "/": """<h1>Notes app</h1>
      <a href="/notes">Notes</a> <a href="/account">Account</a> <a href="/checkout">Upgrade</a>
      <a href="/logout">Log out</a> <a href="https://evil.example.com/steal">Partner</a> <a href="/login">Sign in</a>
      <form method="get" action="/search"><label>Search <input name="q"></label><button>Search</button></form>""",
    "/notes": """<h1>Notes</h1><a href="/notes/1">First</a> <a href="/notes/2">Second</a>
      <form method="post" action="/notes"><label>Title <input name="title" required></label>
        <label>Body <textarea name="body"></textarea></label><button type="submit">Create note</button></form>
      <form method="post" action="/notes/1/delete"><button type="submit">Delete</button></form>
      <button onclick="fetch('/api/notes/delete-all',{method:'POST'})">Delete all</button>
      <button onclick="fetch('/api/notes/refresh',{method:'POST'})">Refresh</button>""",
    "/notes/1": """<h1>First note</h1><a href="/notes/1/remove">Remove</a> <a href="/notes">Back</a>""",
    "/notes/2": """<h1>Second note</h1><a href="/notes">Back</a>""",
    "/account": """<h1>Account</h1><a href="/account/goodbye">Close my account</a>
      <form method="post" action="/account/close"><button>Close account</button></form>
      <form method="post" action="/invites"><label>Email <input type="email" name="email"></label>
        <button>Send invite</button></form>""",
    "/checkout": """<h1>Upgrade</h1><form method="post" action="/pay">
      <label>Card number <input name="cc-number" autocomplete="cc-number"></label>
      <label>CVC <input name="cvc"></label>
      <button>Continue</button></form>""",
    "/login": """<h1>Sign in</h1><form method="post" action="/login">
      <label>Email <input type="email" name="email"></label>
      <label>Password <input type="password" name="password"></label><button>Sign in</button></form>""",
}
DANGEROUS = {
    ("POST", "/notes/1/delete"),
    ("POST", "/api/notes/delete-all"),
    ("GET", "/notes/1/remove"),
    ("POST", "/account/close"),
    ("POST", "/invites"),
    ("POST", "/pay"),
    ("GET", "/logout"),
    ("GET", "/account/goodbye"),
}


class _App:
    def __init__(self) -> None:
        self.hits: list[tuple[str, str, dict[str, list[str]]]] = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _page(self, body: str, code: int = 200) -> None:
                data = f"<!doctype html><title>Notes</title><body>{body}</body>".encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                app.hits.append(("GET", u.path, parse_qs(u.query)))
                if u.path == "/search":
                    self._page(f"<h1>Results for {parse_qs(u.query).get('q', [''])[0]}</h1><a href='/notes'>Notes</a>")
                elif u.path in PAGES:
                    self._page(PAGES[u.path])
                else:
                    self._page("<h1>Not found</h1>", 404)

            def do_POST(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                app.hits.append(("POST", u.path, parse_qs(self.rfile.read(length).decode())))
                if u.path in ("/notes", "/login"):
                    self.send_response(303)
                    self.send_header("Location", "/notes/2")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._page("<h1>Done</h1>")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def _explore(tmp_path: Path, app: _App, **cfg: object) -> dict:
    config = tmp_path / "config.json"
    out = tmp_path / "out.json"
    config.write_text(json.dumps({"base_url": app.url, "max_actions": 60, "max_seconds": 120, **cfg}))
    proc = subprocess.run(["node", str(EXPLORER), str(config), str(out)], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(out.read_text())


def test_exploration_never_triggers_denylisted_actions(tmp_path: Path) -> None:
    app = _App()
    try:
        result = _explore(tmp_path, app, credentials={"username": "qa@example.com", "password": "pw-123456"})
    finally:
        app.server.shutdown()
    hit = {(m, p) for m, p, _ in app.hits}
    assert not hit & DANGEROUS, hit & DANGEROUS
    # Safe parts of the app were explored, including a write (create) and a search.
    assert {("GET", "/notes"), ("GET", "/notes/1"), ("GET", "/account"), ("GET", "/checkout")} <= hit
    assert ("POST", "/notes") in hit and ("GET", "/search") in hit and ("POST", "/api/notes/refresh") in hit
    login = [q for m, p, q in app.hits if (m, p) == ("POST", "/login")]
    assert login and login[0]["email"] == ["qa@example.com"]
    skipped = {
        (a.get("label") or a.get("target"), a["blocked"]) for a in result["actions"] if a["outcome"] == "skipped"
    }
    reasons = {r for _, r in skipped}
    assert {"denylisted action", "denylisted link", "payment form"} <= reasons
    assert (
        ("Continue", "payment form") in skipped
        and ("Log out", "denylisted link") in skipped
        and ("Remove", "denylisted link") in skipped
    )
    assert any(label == "Delete all" for label, _ in skipped)
    # The external link was neither followed nor requested.
    assert all(not p["url"].startswith("http") for p in result["pages"])
    assert {"POST /notes", "POST /api/notes/refresh"} <= {c["key"] for c in result["api_calls"]}
    assert all(
        "password" not in json.dumps(a.get("fields", [])) or "<secret>" in json.dumps(a) for a in result["actions"]
    )
    assert "pw-123456" not in json.dumps(result)


def test_exploration_respects_the_action_budget(tmp_path: Path) -> None:
    app = _App()
    try:
        result = _explore(tmp_path, app, max_actions=3)
    finally:
        app.server.shutdown()
    assert result["stats"]["actions_used"] <= 3 and result["stats"]["budget_exhausted"]
    executed = [a for a in result["actions"] if a["outcome"] != "skipped"]
    assert len(executed) <= 3
    assert len([h for h in app.hits if h[0] == "POST"]) <= 2
