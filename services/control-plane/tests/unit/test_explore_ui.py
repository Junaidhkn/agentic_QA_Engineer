"""Browser exploration → discovery input (FR-8): routes from crawled pages and observed API
calls, reviewable browser candidates from successfully submitted forms."""

from __future__ import annotations

from reliability.agents.discovery import explore_ui
from reliability.modules.workflows.ir import validation_errors

EXPLORATION = {
    "base_url": "https://app.example.com",
    "pages": [
        {"url": "/", "path": "/", "status": 200, "headings": ["Home"]},
        {"url": "/notes", "path": "/notes", "status": 200, "headings": ["Your notes"]},
        {"url": "/notes/{id}", "path": "/notes/{id}", "status": 200, "headings": ["Note saved"]},
        {"url": "/broken", "path": "/broken", "status": 500, "headings": []},
    ],
    "actions": [
        {
            "type": "submit",
            "page": "/login",
            "label": "Sign in",
            "login": True,
            "outcome": "ok",
            "fields": [{"name": "email", "type": "email", "label": "Email"}],
        },
        {
            "type": "submit",
            "page": "/notes",
            "label": "Create note",
            "method": "post",
            "outcome": "ok",
            "url_after": "/notes/{id}",
            "fields": [
                {"name": "title", "type": "text", "label": "Title"},
                {"name": "owner_email", "type": "email", "placeholder": "Owner e-mail"},
                {"name": "pw", "type": "password", "label": "Password"},
                {"name": "nolabel", "type": "text"},
            ],
        },
        {"type": "submit", "page": "/notes", "label": "Create note", "outcome": "ok", "fields": []},
        {"type": "submit", "page": "/search", "label": "Search", "method": "get", "outcome": "ok", "fields": []},
        {"type": "submit", "page": "/notes", "label": "Delete", "outcome": "skipped", "blocked": "denylisted action"},
        {"type": "navigate", "page": "/", "label": "Log out", "outcome": "skipped", "blocked": "denylisted link"},
        {"type": "submit", "page": "/broken", "label": "Retry", "outcome": "error", "fields": []},
    ],
    "api_calls": [{"key": "GET /api/notes"}, {"key": "POST /api/notes/{id}/pin"}, {"key": "bogus"}],
    "blocked_requests": 2,
    "stats": {"actions_used": 6, "action_budget": 40, "budget_exhausted": False},
}


def test_routes_from_pages_and_api_calls() -> None:
    found = {(r.method, r.path, r.kind, r.data_write) for r in explore_ui.routes(EXPLORATION)}
    assert found == {
        ("GET", "/", "page", False),
        ("GET", "/notes", "page", False),
        ("GET", "/notes/{id}", "page", False),
        ("GET", "/api/notes", "api", False),
        ("POST", "/api/notes/{id}/pin", "api", True),
    }
    assert all(r.framework == "browser_exploration" for r in explore_ui.routes(EXPLORATION))


def test_merge_keeps_static_routes_and_adds_only_new_ones() -> None:
    static = [{"method": "GET", "path": "/api/notes", "kind": "api", "framework": "express", "file": "a.js", "line": 3}]
    merged = explore_ui.merge_routes(static, EXPLORATION)
    assert merged[0] == static[0]
    assert sum(1 for r in merged if (r["method"], r["path"]) == ("GET", "/api/notes")) == 1
    assert len(merged) == 5


def test_candidates_are_valid_reviewable_browser_workflows() -> None:
    out = explore_ui.candidates(EXPLORATION, "QA_USER", "QA_PASSWORD")
    keys = [c["ir"]["key"] for c in out]
    # One per distinct successful non-login submit; skipped, failed and login submits excluded.
    assert keys == ["explore.notes.create-note", "explore.search.search"]
    create = out[0]
    ir = create["ir"]
    assert not validation_errors(ir)
    assert create["data_write"] and create["origin"] == "exploration" and create["source"] == "browser"
    assert ir["kind"] == "browser" and ir["source"]["type"] == "browser_exploration"
    assert ir["preconditions"] == [
        {
            "type": "login",
            "username_ref": "secret:QA_USER",
            "credential_ref": "secret:QA_PASSWORD",
            "login_path": "/login",
        }
    ]
    fills = [s for s in ir["steps"] if s["action"] == "fill"]
    # Password and unlabelled fields are never filled with synthetic data.
    assert [f["locator"] for f in fills] == [{"label": "Title"}, {"placeholder": "Owner e-mail"}]
    assert ir["data"]["vars"] == {"title": {"faker": "lorem.word"}, "owner_email": {"faker": "internet.email"}}
    assert ir["steps"][-1]["assertion"] == {"type": "text_visible", "value": "Note saved"}
    search = out[1]
    assert not search["data_write"]
    assert search["ir"]["steps"][-1]["assertion"] == {"type": "url_matches", "value": "/search"}


def test_candidates_without_credentials_have_no_login_precondition() -> None:
    assert all("preconditions" not in c["ir"] for c in explore_ui.candidates(EXPLORATION))


def test_summary_counts_skips_by_reason() -> None:
    s = explore_ui.summary(EXPLORATION)
    assert s["skipped_by_reason"] == {"denylisted action": 1, "denylisted link": 1}
    assert s["blocked_requests"] == 2 and s["actions_used"] == 6 and s["error"] is None
