"""Deterministic workflow synthesis: the baseline every discovery produces without a model.

Endpoints (static routes merged with OpenAPI operations) are grouped by collection path into
resource journeys — create → fetch → list, browse, lifecycle (update/delete, destructive) and
item actions — plus auth journeys and page-load checks. Request bodies come from OpenAPI
examples, Pydantic models or ``req.body`` usage; foreign keys are resolved from a list call on
the referenced collection when one exists. The model step (``graph.synthesize``) can add
multi-resource journeys and better bodies; it never replaces this baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from reliability.agents.discovery.openapi import Operation
from reliability.agents.discovery.routes import Route
from reliability.agents.discovery.snapshot import CodeSnapshot

SKIP_PATHS = re.compile(
    r"^(/api)?/(\.well-known|health|healthz|readyz|livez|ready|metrics|docs|redoc|openapi\.json|favicon\.ico|_next)\b"
)
LOGIN = re.compile(r"/(login|signin|sign-in|token|sessions?|auth/login|auth/token)$", re.I)
SIGNUP = re.compile(r"/(signup|sign-up|register|users|auth/register|auth/signup)$", re.I)
PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
NAME_FIELDS = re.compile(r"^(name|full_?name|first_?name|last_?name|username|display_?name)$", re.I)
MAX_PAGES = 15


@dataclass
class Endpoint:
    method: str
    path: str
    body_fields: dict[str, str] = field(default_factory=dict)
    body_example: Any = None
    auth: bool = False
    data_write: bool = False
    evidence: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    handler_file: str | None = None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class Candidate:
    ir: dict[str, Any]
    confidence: float
    data_write: bool
    source: str  # static | openapi

    @property
    def key(self) -> str:
        return str(self.ir["key"])


def merge_endpoints(routes: list[Route], operations: list[Operation]) -> list[Endpoint]:
    endpoints: dict[str, Endpoint] = {}
    for r in routes:
        if r.kind != "api" or SKIP_PATHS.match(r.path):
            continue
        ep = endpoints.setdefault(r.key, Endpoint(r.method, r.path, handler_file=r.handler_file))
        ep.body_fields = ep.body_fields or dict(r.body_fields)
        ep.auth |= r.auth_hint
        ep.data_write |= r.data_write
        ep.evidence.append(f"file:{r.handler_file}:{r.line}")
        ep.sources.add("static")
    for op in operations:
        if SKIP_PATHS.match(op.path):
            continue
        key = f"{op.method} {op.path}"
        ep = endpoints.setdefault(key, Endpoint(op.method, op.path, data_write=op.method != "GET"))
        ep.body_fields = op.body_fields or ep.body_fields
        ep.body_example = op.body_example
        ep.auth |= op.secured
        ep.evidence.append(f"openapi:{op.operation_id or key}")
        ep.sources.add("openapi")
    for ep in endpoints.values():
        ep.evidence.insert(0, f"route:{ep.key}")
    return sorted(endpoints.values(), key=lambda e: (e.path, e.method))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "root"


def _var(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)
    return ("v_" + s if not s or not s[0].isalpha() else s)[:40]


def id_var(collection: str) -> str:
    return _var(resource_name(collection).replace("-", "_").replace(".", "_") + "_id")


def collection_of(path: str) -> str:
    """``/api/orders/{id}`` → ``/api/orders``; ``/api/orders`` → itself."""
    parts = path.rstrip("/").split("/")
    while parts and PARAM.fullmatch(parts[-1] or ""):
        parts.pop()
    return "/".join(parts) or "/"


def resource_name(collection: str) -> str:
    parts = [p for p in collection.split("/") if p and not PARAM.fullmatch(p) and p not in ("api",)]
    parts = [p for p in parts if not re.fullmatch(r"v\d+", p)]
    return slug(".".join(parts[-2:]) if parts else "root").replace("_", "-") or "root"


class _Builder:
    """Accumulates vars and steps for one workflow, resolving body values and foreign keys."""

    def __init__(self, by_key: dict[str, Endpoint]) -> None:
        self.by_key = by_key
        self.vars: dict[str, Any] = {}
        self.steps: list[dict[str, Any]] = []
        self.defined: set[str] = set()
        self.evidence: list[str] = []
        self.data_write = False
        self.auth = False

    def _value(self, name: str, kind: str) -> Any:
        var = _var(name)
        if kind == "email":
            self.vars.setdefault(var, {"faker": "internet.email"})
        elif kind == "int":
            if name.lower().endswith("_id") or name.endswith("Id"):
                ref = self._lookup_fk(name)
                if ref:
                    return "{{ " + ref + " }}"
                return 1
            self.vars.setdefault(var, {"range": [1, 5]})
        elif kind == "float":
            return 1.5
        elif kind == "bool":
            return True
        else:
            if name.lower().endswith("_id") or name.endswith("Id"):
                ref = self._lookup_fk(name)
                if ref:
                    return "{{ " + ref + " }}"
            if "password" in name.lower():
                self.vars.setdefault(var, {"value": "Reliability-Test-Pw1!"})
            else:
                self.vars.setdefault(var, {"faker": "person.name" if NAME_FIELDS.match(name) else "lorem.word"})
        self.defined.add(var)
        return "{{ " + var + " }}"

    def _lookup_fk(self, field_name: str) -> str | None:
        base = re.sub(r"(_id|Id)$", "", field_name).lower()
        var = _var(field_name)
        if var in self.defined:
            return var
        for ep in self.by_key.values():
            if ep.method == "GET" and not PARAM.search(ep.path):
                last = ep.path.rstrip("/").rsplit("/", 1)[-1].lower()
                if last in (base, base + "s", base + "es") or last.rstrip("s") == base:
                    self.add(ep, extract={var: "$[0].id"}, step_id=f"list_{slug(last)}")
                    return var
        return None

    def body(self, ep: Endpoint) -> Any:
        if ep.body_example is not None:
            return ep.body_example
        if not ep.body_fields:
            return None
        return {name: self._value(name, kind) for name, kind in ep.body_fields.items()}

    def add(
        self,
        ep: Endpoint,
        *,
        path: str | None = None,
        extract: dict[str, str] | None = None,
        step_id: str | None = None,
        status: list[int] | None = None,
    ) -> None:
        request: dict[str, Any] = {"method": ep.method, "path": path or ep.path}
        if ep.method in ("POST", "PUT", "PATCH"):
            body = self.body(ep)
            if body is not None:
                request["json"] = body
        step: dict[str, Any] = {"id": step_id or self._step_id(ep), "name": ep.key, "request": request}
        if extract:
            step["extract"] = extract
            self.defined |= set(extract)
        step["expect"] = {
            "status": status or ([200, 201] if ep.method == "POST" else [200, 204] if ep.method == "DELETE" else [200])
        }
        self.steps.append(step)
        self.evidence += ep.evidence[:2]
        self.data_write |= ep.data_write or ep.method != "GET"
        self.auth |= ep.auth

    def resolve(self, path: str, depth: int = 0) -> str | None:
        """Replace each ``{param}`` with a variable produced by an earlier step: the id of a
        created (``POST``) or listed (``GET``) parent item. ``None`` when not resolvable."""
        out = path
        for m in PARAM.finditer(path):
            parent = path[: m.start()].rstrip("/")
            var = id_var(collection_of(parent))
            if var not in self.defined:
                create = self.by_key.get(f"POST {parent}")
                lst = self.by_key.get(f"GET {parent}")
                if depth > 3 or (create is None and lst is None):
                    return None
                ep = create or lst
                assert ep is not None
                resolved = self.resolve(ep.path, depth + 1)
                if resolved is None:
                    return None
                self.add(
                    ep,
                    path=resolved,
                    extract={var: "$.id" if create else "$[0].id"},
                    step_id=("create_" if create else "list_") + slug(parent.rsplit("/", 1)[-1])[:30],
                )
            out = out.replace(m.group(0), "{{ " + var + " }}", 1)
        return out

    def _step_id(self, ep: Endpoint) -> str:
        base = slug(f"{ep.method}_{ep.path.rsplit('/', 1)[-1] or 'root'}")[:30]
        existing = {s["id"] for s in self.steps}
        candidate, n = base, 2
        while candidate in existing:
            candidate, n = f"{base}_{n}", n + 1
        return candidate


def _login_precondition(login: Endpoint, snapshot: CodeSnapshot | None) -> dict[str, Any]:
    handler = snapshot.read(login.handler_file) if snapshot and login.handler_file else ""
    token_path = "$.access_token" if "access_token" in handler or login.path.endswith("token") else "$.token"
    user_field = next((f for f in login.body_fields if "email" in f.lower()), None) or next(
        (f for f in login.body_fields if "user" in f.lower()), "email"
    )
    request: dict[str, Any] = {"method": "POST", "path": login.path}
    if "OAuth2PasswordRequestForm" in handler:
        request["form"] = {"username": "{{ username }}", "password": "{{ password }}"}
    else:
        request["json"] = {user_field: "{{ username }}", "password": "{{ password }}"}
    return {
        "type": "login",
        "username_ref": "secret:TEST_USER_EMAIL",
        "credential_ref": "secret:TEST_USER_PASSWORD",
        "login_path": login.path,
        "api": {"id": "login", "request": request, "extract": {"token": token_path}, "expect": {"status": [200]}},
    }


AUTH_SECRETS = {"TEST_USER_EMAIL", "TEST_USER_PASSWORD"}


@dataclass
class InlineAuth:
    """Sign-up + log-in steps prepended to authenticated workflows when no test-user secrets
    are configured, so generated tests are self-contained."""

    steps: list[dict[str, Any]]
    vars: dict[str, Any]


def _ir(
    key: str, title: str, b: _Builder, source: str, login: dict[str, Any] | None, inline: InlineAuth | None = None
) -> dict[str, Any]:
    ir: dict[str, Any] = {
        "ir_version": "1.0",
        "key": key[:120],
        "title": title[:200],
        "kind": "api",
        "criticality": "normal",
        "source": {"type": source, "evidence": list(dict.fromkeys(b.evidence))[:50]},
        "steps": b.steps,
    }
    if b.vars:
        ir["data"] = {"vars": b.vars}
    if b.auth and inline is not None:
        header = {"Authorization": "Bearer {{ token }}"}
        steps = [
            {**st, "request": {**st["request"], "headers": {**st["request"].get("headers", {}), **header}}}
            for st in b.steps
        ]
        ir["steps"] = [*inline.steps, *steps]
        ir["data"] = {"vars": {**inline.vars, **b.vars}}
    elif b.auth and login is not None:
        ir["preconditions"] = [login]
    if any(s["request"]["method"] == "DELETE" for s in b.steps):
        ir["destructive"] = True
    return ir


def _confidence(eps: list[Endpoint]) -> float:
    if all("openapi" in e.sources and "static" in e.sources for e in eps):
        return 0.85
    if all(e.body_example is not None or e.method in ("GET", "DELETE") or e.body_fields for e in eps):
        return 0.7
    return 0.5


def _build(by_key: dict[str, Endpoint], coll: str, roles: dict[int, str], parts: list[Endpoint]) -> _Builder | None:
    """Steps for one journey; the collection's create (or list, when followed by a fetch)
    step extracts the item id that later item paths use."""
    b = _Builder(by_key)
    for i, ep in enumerate(parts):
        path = b.resolve(ep.path)
        if path is None:
            return None
        role = roles.get(id(ep))
        extract = None
        if role == "create":
            extract = {id_var(coll): "$.id"}
        elif role == "list" and any(roles.get(id(p)) == "fetch" for p in parts[i + 1 :]):
            extract = {id_var(coll): "$[0].id"}
        b.add(ep, path=path, extract=extract, step_id=role)
    return b


def synthesize(
    routes: list[Route],
    operations: list[Operation],
    snapshot: CodeSnapshot | None = None,
    secret_names: set[str] | frozenset[str] = frozenset(),
) -> list[Candidate]:
    endpoints = merge_endpoints(routes, operations)
    by_key = {e.key: e for e in endpoints}
    login = next((e for e in endpoints if e.method == "POST" and LOGIN.search(e.path)), None)
    signup = next(
        (
            e
            for e in endpoints
            if e.method == "POST" and SIGNUP.search(e.path) and any("password" in f.lower() for f in e.body_fields)
        ),
        None,
    )
    pre = _login_precondition(login, snapshot) if login else None
    out: list[Candidate] = []
    covered: set[str] = set()
    inline: InlineAuth | None = None

    def emit(key: str, title: str, b: _Builder, eps: list[Endpoint]) -> None:
        source = "openapi" if all("openapi" in e.sources and "static" not in e.sources for e in eps) else "static"
        out.append(Candidate(_ir(key, title, b, source, pre, inline), _confidence(eps), b.data_write, source))
        covered.update(e.key for e in eps)

    # Auth journeys
    if signup and login:
        b = _Builder(by_key)
        b.add(signup, step_id="signup")
        user_field = next((f for f in login.body_fields if "email" in f.lower() or "user" in f.lower()), "email")
        signup_user = next((_var(f) for f in signup.body_fields if "email" in f.lower() or "user" in f.lower()), None)
        b.steps.append(
            {
                "id": "login",
                "name": login.key,
                "request": {
                    "method": "POST",
                    "path": login.path,
                    "json": {
                        user_field: "{{ " + (signup_user or "email") + " }}",
                        "password": "{{ "
                        + next((_var(f) for f in signup.body_fields if "password" in f.lower()), "password")
                        + " }}",
                    },
                },
                "extract": {"token": pre["api"]["extract"]["token"] if pre else "$.token"},
                "expect": {"status": [200]},
            }
        )
        if signup_user is None:
            b.vars["email"] = {"faker": "internet.email"}
        if not AUTH_SECRETS <= set(secret_names):
            inline = InlineAuth([dict(st) for st in b.steps], dict(b.vars))
        b.evidence += login.evidence[:2]
        emit("auth.signup-and-login", "Sign up and log in", b, [signup, login])
    elif login:
        b = _Builder(by_key)
        b.vars = {"username": {"secret": "TEST_USER_EMAIL"}, "password": {"secret": "TEST_USER_PASSWORD"}}
        assert pre is not None
        b.steps.append({**pre["api"], "name": login.key})
        b.evidence += login.evidence[:2]
        emit("auth.login", "Log in", b, [login])

    def journey(key: str, title: str, parts: list[Endpoint], coll: str, roles: dict[int, str]) -> None:
        b = _build(by_key, coll, roles, parts)
        if b is not None:
            emit(key, title, b, parts)

    # Resource journeys
    groups: dict[str, list[Endpoint]] = {}
    for e in endpoints:
        if e.key not in covered:
            groups.setdefault(collection_of(e.path), []).append(e)
    for coll, eps in sorted(groups.items()):
        name = resource_name(coll)
        create = by_key.get(f"POST {coll}")
        lst = by_key.get(f"GET {coll}")
        item_eps = [e for e in eps if e.path != coll]
        item_get = next(
            (
                e
                for e in item_eps
                if e.method == "GET"
                and e.path.count("{") == coll.count("{") + 1
                and PARAM.fullmatch(e.path.rsplit("/", 1)[-1])
            ),
            None,
        )
        mutations = [
            e for e in item_eps if e.method in ("PUT", "PATCH", "DELETE") and PARAM.fullmatch(e.path.rsplit("/", 1)[-1])
        ]
        actions = [e for e in item_eps if e is not item_get and e not in mutations]
        label = name.replace("-", " ")

        roles = {id(e): r for e, r in ((create, "create"), (item_get, "fetch"), (lst, "list")) if e is not None}
        if create:
            journey(
                f"{name}.create", f"Create and fetch {label}", [create, *[e for e in (item_get, lst) if e]], coll, roles
            )
            if mutations:
                journey(
                    f"{name}.lifecycle",
                    f"Update and delete {label}",
                    [create, *sorted(mutations, key=lambda e: e.method == "DELETE")],
                    coll,
                    roles,
                )
        elif lst:
            journey(f"{name}.browse", f"Browse {label}", [lst, *[e for e in (item_get,) if e]], coll, roles)
        for action in actions:
            last = slug(action.path.rsplit("/", 1)[-1].strip("{}"))
            journey(
                f"{name}.{slug(action.method)}-{last}"[:120], f"{action.method} {label} {last}", [action], coll, roles
            )
        if create and PARAM.search(coll) and not lst and not item_get:
            # A POST on a nested path with no siblings is an action on the parent item.
            parent = resource_name(collection_of(coll.rsplit("/", 1)[0]))
            last = slug(coll.rsplit("/", 1)[-1])
            for c in out:
                if c.key == f"{name}.create":
                    c.ir["key"], c.ir["title"] = f"{parent}.{last}"[:120], f"{last.capitalize()} {parent}"
        for e in eps:
            if e.key not in covered and e not in actions:
                journey(f"{name}.{slug(e.method)}"[:120], f"{e.method} {e.path}", [e], coll, roles)

    # Page loads (browser)
    pages = [r for r in routes if r.kind == "page" and not PARAM.search(r.path) and not SKIP_PATHS.match(r.path)]
    for r in pages[:MAX_PAGES]:
        key = "page." + (slug(r.path.strip("/").replace("/", ".")) if r.path != "/" else "home").replace("_", "-")
        ir: dict[str, Any] = {
            "ir_version": "1.0",
            "key": key[:120],
            "title": f"Open {r.path}",
            "kind": "browser",
            "criticality": "normal",
            "source": {"type": "static", "evidence": [f"route:GET {r.path}", f"file:{r.handler_file}:{r.line}"]},
            "steps": [
                {"id": "open", "action": "navigate", "url": r.path},
                {"id": "loaded", "action": "expect", "assertion": {"type": "url_matches", "value": r.path}},
            ],
        }
        if r.auth_hint and login is not None:
            login_page = next((p.path for p in routes if p.kind == "page" and LOGIN.search(p.path)), None)
            if login_page:
                ir["preconditions"] = [
                    {
                        "type": "login",
                        "username_ref": "secret:TEST_USER_EMAIL",
                        "credential_ref": "secret:TEST_USER_PASSWORD",
                        "login_path": login_page,
                    }
                ]
        out.append(Candidate(ir, 0.5, False, "static"))
    # Unique keys
    seen: dict[str, int] = {}
    for c in out:
        n = seen.get(c.key, 0)
        seen[c.key] = n + 1
        if n:
            c.ir["key"] = f"{c.key}-{n + 1}"[:120]
    return out
