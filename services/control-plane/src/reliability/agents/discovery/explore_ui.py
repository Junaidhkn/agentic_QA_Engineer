"""Browser exploration → discovery input (FR-8, agent-workflows §3 ``explore_ui``).

The crawl itself runs on the execution plane (``runners/images/playwright/explore.cjs``): a
bounded, deterministic breadth-first walk of the verified target with an action budget, a
same-origin guard and a denylist of destructive actions that are recorded but never executed.
This module turns its result into what the rest of discovery understands:

* pages and observed API calls become routes (``framework = "browser_exploration"``), so
  page-load and API workflows are synthesized like statically found ones;
* every form the crawler submitted successfully becomes a reviewable browser-workflow candidate
  (navigate → fill → submit → expect), with synthetic data and the test login as a precondition.

Candidates are never tested until a person approves them (ADR-0013).
"""

from __future__ import annotations

import re
from typing import Any

from reliability.agents.discovery.routes import Route
from reliability.modules.workflows.ir import validation_errors

FRAMEWORK = "browser_exploration"
DEFAULT_ACTIONS = 150
DEFAULT_SECONDS = 900
MAX_CANDIDATES = 20
EMAIL = re.compile(r"e-?mail", re.I)
NAME = re.compile(r"name", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "root"


def _path(p: str) -> str:
    return re.sub(r"\{id\}", "{id}", p or "/").split("?", 1)[0] or "/"


def routes(exploration: dict[str, Any]) -> list[Route]:
    out: dict[str, Route] = {}
    for page in exploration.get("pages") or []:
        path = _path(page.get("path") or page.get("url") or "/")
        if (page.get("status") or 200) < 400:
            out.setdefault(f"GET {path}", Route("GET", path, "page", FRAMEWORK, "exploration", 0))
    for call in exploration.get("api_calls") or []:
        method, _, path = str(call.get("key", "")).partition(" ")
        if method and path.startswith("/"):
            out.setdefault(
                f"{method} {path}",
                Route(
                    method, _path(path), "api", FRAMEWORK, "exploration", 0, data_write=method not in ("GET", "HEAD")
                ),
            )
    return list(out.values())


def merge_routes(existing: list[dict[str, Any]], exploration: dict[str, Any]) -> list[dict[str, Any]]:
    from reliability.agents.discovery.graph import path_shape

    seen = {(r["method"], path_shape(r["path"]), r["kind"]) for r in existing}
    added = [
        r.to_dict()
        for r in routes(exploration)
        if (r.method, path_shape(r.path), r.kind) not in seen
        # API calls the browser made that static analysis missed; pages the crawler reached.
    ]
    return existing + added


def _locator(field: dict[str, Any]) -> dict[str, str] | None:
    if field.get("label"):
        return {"label": str(field["label"])[:200]}
    if field.get("placeholder"):
        return {"placeholder": str(field["placeholder"])[:200]}
    return None


def candidates(
    exploration: dict[str, Any], username_secret: str | None = None, password_secret: str | None = None
) -> list[dict[str, Any]]:
    """Reviewable browser workflows for the forms the crawl submitted successfully."""
    login = next(
        (
            a
            for a in exploration.get("actions") or []
            if a.get("type") == "submit" and a.get("login") and a.get("outcome") == "ok"
        ),
        None,
    )
    headings = {p.get("path"): (p.get("headings") or [None])[0] for p in exploration.get("pages") or []}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in exploration.get("actions") or []:
        if a.get("type") != "submit" or a.get("outcome") != "ok" or a.get("login"):
            continue
        page, label = _path(a.get("page") or "/"), str(a.get("label") or "Submit")[:120]
        key = f"explore.{_slug(page)}.{_slug(label)}"[:120]
        if key in seen:
            continue
        seen.add(key)
        steps: list[dict[str, Any]] = [{"id": "open", "action": "navigate", "url": page}]
        variables: dict[str, Any] = {}
        for i, field in enumerate(a.get("fields") or []):
            loc = _locator(field)
            if loc is None or field.get("type") in ("checkbox", "radio", "select-one", "select", "password"):
                continue
            var = re.sub(r"[^a-z0-9_]", "_", str(field.get("name") or f"field_{i}").lower())[:40] or f"field_{i}"
            if not re.match(r"^[a-z_]", var):
                var = f"f_{var}"[:40]
            hint = f"{field.get('name')} {field.get('label')} {field.get('type')}"
            variables[var] = {
                "faker": "internet.email"
                if EMAIL.search(hint)
                else "person.name"
                if NAME.search(hint)
                else "lorem.word"
            }
            steps.append({"id": f"fill-{i}", "action": "fill", "locator": loc, "value": "{{ " + var + " }}"})
        steps.append({"id": "submit", "action": "click", "locator": {"role": "button", "name": label}})
        after = _path(a.get("url_after") or page)
        heading = headings.get(after)
        if heading:
            steps.append(
                {"id": "done", "action": "expect", "assertion": {"type": "text_visible", "value": str(heading)[:300]}}
            )
        elif "{id}" not in after:
            steps.append({"id": "done", "action": "expect", "assertion": {"type": "url_matches", "value": after}})
        ir: dict[str, Any] = {
            "ir_version": "1.0",
            "key": key,
            "title": f"{label} on {page}"[:200],
            "kind": "browser",
            "criticality": "normal",
            "source": {
                "type": "browser_exploration",
                "evidence": [f"page:{page}", f"action:{label}", f"result:{after}"],
            },
            "steps": steps,
        }
        if variables:
            ir["data"] = {"vars": variables}
        if login and username_secret and password_secret:
            ir["preconditions"] = [
                {
                    "type": "login",
                    "username_ref": f"secret:{username_secret}",
                    "credential_ref": f"secret:{password_secret}",
                    "login_path": _path(login.get("page") or "/login"),
                }
            ]
        if not validation_errors(ir):
            out.append(
                {
                    "ir": ir,
                    "confidence": 0.6,
                    "data_write": str(a.get("method", "post")).lower() != "get",
                    "source": "browser",
                    "origin": "exploration",
                }
            )
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def summary(exploration: dict[str, Any]) -> dict[str, Any]:
    acts = exploration.get("actions") or []
    return {
        **(exploration.get("stats") or {}),
        "skipped_by_reason": {
            r: sum(1 for a in acts if a.get("blocked") == r)
            for r in sorted({a["blocked"] for a in acts if a.get("blocked")})
        },
        "blocked_requests": exploration.get("blocked_requests", 0),
        "error": exploration.get("error"),
    }
