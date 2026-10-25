"""Deterministic sandbox build plans (ADR-0011, low-level-design §5).

A plan says how to build and start the customer's app in a sandbox and what it needs around
it. Strategy, in order of preference: the repository's ``docker-compose`` file, its
``Dockerfile``, else a platform template for the detected stack (Node: Next.js/Express;
Python: FastAPI and generic ASGI/WSGI). Databases from compose, or an ephemeral Postgres/Redis
when an ORM or driver is detected, with migrations (Prisma, Drizzle, Alembic, Django) and the
project's seed script. External HTTP dependencies configured through environment variables
are pointed at the stub server. Commands are argv lists — never shell strings.

The plan is data: the runner executes it (``runner_agent.sandbox_env``) and the UI shows it.
Apps that need something we cannot provide get ``unsupported`` with the reason, so the user
can fall back to a preview URL (ADR-0011).
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

from reliability.agents.discovery import routes as routes_mod
from reliability.agents.discovery import stack as stack_mod
from reliability.agents.discovery.snapshot import CodeSnapshot

PLAN_VERSION = 1
POSTGRES_IMAGE = "postgres:16.6"
REDIS_IMAGE = "redis:7.4.1"
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
HEALTH_CANDIDATES = ("/health", "/healthz", "/api/health", "/api/healthz", "/readyz", "/status")
DB_ENV_NAMES = ("DATABASE_URL", "POSTGRES_URL", "DB_URL", "SQLALCHEMY_DATABASE_URI", "PG_CONNECTION_STRING")
ENV_REF = re.compile(
    r"""(?:process\.env\.([A-Z][A-Z0-9_]{2,})|process\.env\[['"]([A-Z][A-Z0-9_]{2,})['"]\]|"""
    r"""os\.environ\[['"]([A-Z][A-Z0-9_]{2,})['"]\]|os\.(?:environ\.get|getenv)\(\s*['"]([A-Z][A-Z0-9_]{2,})['"])"""
)
STUB_ENV = re.compile(
    r"^(?!DATABASE|POSTGRES|REDIS|OTEL|NEXT_PUBLIC_|PUBLIC_|PORT$|HOST$)[A-Z0-9_]*_(?:API_)?(?:URL|BASE_URL|BASE|ENDPOINT|HOST)$"
)
EXTERNAL_HOST = re.compile(r"""['"`]https://((?:api|hooks|graph|www)\.[a-z0-9.-]+\.[a-z]{2,})[/'"`]""")
PLATFORM_ENV = {"PORT", "HOST", "NODE_ENV", "PYTHONPATH", "PATH", "HOME", "CI", "DEBUG", "TZ", "LOG_LEVEL"}
CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
LISTEN_ALL = "0.0.0.0"  # noqa: S104 - the app listens inside its sandbox network namespace


@dataclass
class Stub:
    name: str
    env: str
    routes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BuildPlan:
    strategy: str  # compose | dockerfile | template
    root: str = ""
    language: str | None = None
    framework: str | None = None
    compose_file: str | None = None
    app_service: str = "app"
    dockerfile: str | None = None
    template: str | None = None  # python | node
    install: list[list[str]] = field(default_factory=list)
    build: list[list[str]] = field(default_factory=list)
    start: list[str] | None = None
    port: int = 8000
    health_path: str = "/"
    env: dict[str, str] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    database_env: list[str] = field(default_factory=list)
    database_scheme: str = "postgresql"
    migrate: list[list[str]] = field(default_factory=list)
    seed: list[list[str]] = field(default_factory=list)
    stubs: list[Stub] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: str | None = None
    plan_version: int = PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rel(root: str, name: str) -> str:
    return f"{root}/{name}" if root else name


def _pkg(snapshot: CodeSnapshot, root: str) -> dict[str, Any]:
    try:
        data = json.loads(snapshot.read(_rel(root, "package.json")) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pyproject(snapshot: CodeSnapshot, root: str) -> dict[str, Any]:
    try:
        return tomllib.loads(snapshot.read(_rel(root, "pyproject.toml")) or "")
    except tomllib.TOMLDecodeError:
        return {}


def _files_under(snapshot: CodeSnapshot, root: str) -> list[str]:
    prefix = f"{root}/" if root else ""
    return [p for p in snapshot.files if p.startswith(prefix)]


def _env_refs(snapshot: CodeSnapshot, files: list[str]) -> dict[str, bool]:
    """Environment variable names the code reads → whether a default is supplied."""
    refs: dict[str, bool] = {}
    for path in files:
        if not path.endswith((".py", ".js", ".ts", ".mjs", ".cjs", ".tsx")):
            continue
        text = snapshot.read(path)
        for m in ENV_REF.finditer(text):
            name = next(g for g in m.groups() if g)
            optional = "getenv(" in m.group(0) or ".get(" in m.group(0) or "process.env" in m.group(0)
            refs[name] = refs.get(name, True) and optional
    return refs


def _python_module_app(snapshot: CodeSnapshot, root: str) -> tuple[str, str] | None:
    """(module, variable) of the ASGI app: ``x = FastAPI(...)`` (preferring main/app modules)."""
    best: tuple[int, str, str] | None = None
    for path in _files_under(snapshot, root):
        if not path.endswith(".py") or "/tests/" in f"/{path}" or "/migrations/" in path or "/alembic/" in path:
            continue
        try:
            tree = ast.parse(snapshot.read(path))
        except SyntaxError:
            continue
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name | ast.Attribute)
                and (node.value.func.id if isinstance(node.value.func, ast.Name) else node.value.func.attr)
                in ("FastAPI", "Starlette", "Flask")
                and isinstance(node.targets[0], ast.Name)
            ):
                rel = path[len(root) + 1 :] if root else path
                module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                score = (0 if PurePosixPath(rel).stem in ("main", "app", "server", "asgi") else 1) + rel.count("/")
                if best is None or score < best[0]:
                    best = (score, module, node.targets[0].id)
    return (best[1], best[2]) if best else None


def _python_deps(snapshot: CodeSnapshot, root: str) -> list[str]:
    req = snapshot.read(_rel(root, "requirements.txt"))
    if req:
        return [line.strip() for line in req.splitlines() if line.strip() and not line.strip().startswith(("#", "-"))]
    project = _pyproject(snapshot, root).get("project") or {}
    return [str(d) for d in project.get("dependencies") or []]


def _health(found: list[routes_mod.Route]) -> str:
    gets = {r.path for r in found if r.method == "GET" and r.kind == "api"}
    for cand in HEALTH_CANDIDATES:
        if cand in gets:
            return cand
    pages = {r.path for r in found if r.kind == "page"}
    if "/" in pages or "/" in gets:
        return "/"
    simple = sorted(p for p in gets if "{" not in p)
    return simple[0] if simple else "/"


def _port_from_compose(service: dict[str, Any]) -> int | None:
    for entry in [*(service.get("ports") or []), *(service.get("expose") or [])]:
        if isinstance(entry, dict):
            target = entry.get("target")
            if target:
                return int(target)
            continue
        text = str(entry).split("/")[0]
        container = text.rsplit(":", 1)[-1]
        if container.isdigit():
            return int(container)
    return None


def _port_from_dockerfile(text: str) -> int | None:
    m = re.search(r"^\s*EXPOSE\s+(\d+)", text, re.M | re.I)
    return int(m.group(1)) if m else None


def _compose_plan(plan: BuildPlan, snapshot: CodeSnapshot, name: str) -> None:
    try:
        doc = yaml.safe_load(snapshot.read(name)) or {}
    except yaml.YAMLError as exc:
        plan.warnings.append(f"{name} is not valid YAML ({exc}); falling back")
        plan.strategy = ""
        return
    services: dict[str, Any] = doc.get("services") or {}
    if not services:
        plan.strategy = ""
        return
    plan.compose_file = name
    app = next((n for n, s in services.items() if isinstance(s, dict) and s.get("build")), None)
    if app is None:
        plan.warnings.append(f"{name} has no service with a build section; the app is assumed to be prebuilt")
        app = next(iter(services))
    plan.app_service = app
    plan.port = _port_from_compose(services[app]) or plan.port
    for svc_name, svc in services.items():
        image = str((svc or {}).get("image", ""))
        if image.startswith(("postgres", "postgis/")):
            env = svc.get("environment") or {}
            if isinstance(env, list):
                env = dict(e.split("=", 1) for e in env if "=" in e)
            plan.services["postgres"] = {
                "from_compose": svc_name,
                "user": env.get("POSTGRES_USER", "postgres"),
                "database": env.get("POSTGRES_DB", env.get("POSTGRES_USER", "postgres")),
            }
        elif image.startswith("redis"):
            plan.services["redis"] = {"from_compose": svc_name}


def plan_for(snapshot: CodeSnapshot, secret_names: set[str] | None = None) -> BuildPlan:
    st = stack_mod.detect(snapshot)
    found = routes_mod.extract(snapshot, st)
    apps = [a for a in st.apps if a.frameworks] or st.apps
    plan = BuildPlan(strategy="")
    if not apps and not any(PurePosixPath(p).name in COMPOSE_NAMES for p in snapshot.files):
        plan.unsupported = "no supported application found (expected package.json or a Python project)"
        return plan
    app = sorted(apps, key=lambda a: (a.root.count("/"), a.root))[0] if apps else None
    root = app.root if app else ""
    plan.root, plan.language = root, app.language if app else None
    plan.framework = app.frameworks[0] if app and app.frameworks else None
    plan.health_path = _health(found)

    compose = next((n for n in (_rel(root, c) for c in COMPOSE_NAMES) if n in snapshot.files), None)
    compose = compose or next((c for c in COMPOSE_NAMES if c in snapshot.files), None)
    if compose:
        plan.strategy = "compose"
        _compose_plan(plan, snapshot, compose)
    if not plan.strategy and _rel(root, "Dockerfile") in snapshot.files:
        plan.strategy, plan.dockerfile = "dockerfile", _rel(root, "Dockerfile")
        plan.port = _port_from_dockerfile(snapshot.read(plan.dockerfile)) or plan.port
    if app is None:
        return plan

    files = _files_under(snapshot, root)
    plan.strategy = plan.strategy or "template"
    if app.language == "javascript":
        _node(plan, snapshot, app, root)
    elif app.language == "python":
        _python(plan, snapshot, app, root, files)
    _datastores(plan, snapshot, app, root, files)
    _stubs_and_env(plan, snapshot, files, secret_names or set())
    if plan.strategy == "template" and plan.start is None:
        plan.unsupported = plan.unsupported or "could not determine how to start the app"
    return plan


def _node(plan: BuildPlan, snapshot: CodeSnapshot, app: stack_mod.App, root: str) -> None:
    pkg = _pkg(snapshot, root)
    scripts: dict[str, str] = pkg.get("scripts") or {}
    pm = app.package_manager or "npm"
    plan.template = "node"
    plan.install = [
        {"pnpm": ["pnpm", "install", "--frozen-lockfile"], "yarn": ["yarn", "install", "--frozen-lockfile"]}.get(
            pm,
            ["npm", "ci"]
            if any(_rel(root, "package-lock.json") == lf for lf in snapshot.lockfiles)
            else ["npm", "install"],
        )
    ]
    if "build" in scripts:
        plan.build = [[pm, "run", "build"]]
    if "nextjs" in app.frameworks:
        plan.start = [pm, "run", "start"] if "start" in scripts else ["npx", "next", "start", "-p", "{port}"]
        plan.env.setdefault("NODE_ENV", "production")
    elif "start" in scripts:
        plan.start = [pm, "run", "start"]
    else:
        main = pkg.get("main") or next(
            (f for f in ("server.js", "app.js", "index.js") if _rel(root, f) in snapshot.files), None
        )
        plan.start = ["node", main] if main else None
    if plan.strategy == "template":
        plan.port = 3000
    plan.env.setdefault("PORT", "{port}")


def _python(plan: BuildPlan, snapshot: CodeSnapshot, app: stack_mod.App, root: str, files: list[str]) -> None:
    plan.template = "python"
    deps = _python_deps(snapshot, root)
    if snapshot.read(_rel(root, "requirements.txt")):
        plan.install = [["pip", "install", "--no-cache-dir", "-r", "requirements.txt"]]
    elif deps:
        plan.install = [["pip", "install", "--no-cache-dir", *deps]]
    if "django" in app.frameworks and _rel(root, "manage.py") in snapshot.files:
        plan.start = ["python", "manage.py", "runserver", f"{LISTEN_ALL}:{{port}}", "--noreload"]
        plan.migrate.append(["python", "manage.py", "migrate", "--noinput"])
    else:
        target = _python_module_app(snapshot, root)
        if target:
            module, var = target
            server = "uvicorn" if "flask" not in app.frameworks else "gunicorn"
            plan.start = (
                ["python", "-m", "uvicorn", f"{module}:{var}", "--host", LISTEN_ALL, "--port", "{port}"]
                if server == "uvicorn"
                else ["gunicorn", "-b", f"{LISTEN_ALL}:{{port}}", f"{module}:{var}"]
            )
            if not any("uvicorn" in d.lower() or "gunicorn" in d.lower() for d in deps):
                plan.warnings.append(f"{server} is not a declared dependency; the sandbox installs it")
                plan.install.append(["pip", "install", "--no-cache-dir", server])
    plan.port = 8000 if plan.strategy == "template" else plan.port


def _datastores(plan: BuildPlan, snapshot: CodeSnapshot, app: stack_mod.App, root: str, files: list[str]) -> None:
    needs_pg = "postgresql" in app.databases or bool(
        {"prisma", "drizzle", "sequelize", "typeorm", "knex", "django"} & set(app.orm)
    )
    needs_pg = needs_pg or (
        "sqlalchemy" in app.orm
        and any("postgres" in d.lower() or "psycopg" in d.lower() for d in _python_deps(snapshot, root))
    )
    if needs_pg and "postgres" not in plan.services:
        plan.services["postgres"] = {
            "image": POSTGRES_IMAGE,
            "user": "sandbox",
            "database": "app",
            "reason": ", ".join(sorted(set(app.orm) | set(app.databases))),
        }
    if "redis" in app.databases and "redis" not in plan.services:
        plan.services["redis"] = {"image": REDIS_IMAGE}
    if "mongodb" in app.databases or "mongoose" in app.orm:
        plan.warnings.append("MongoDB is not provisioned in sandboxes yet; data calls will fail")
    if "postgres" in plan.services:
        refs = _env_refs(snapshot, files)
        plan.database_env = [n for n in DB_ENV_NAMES if n in refs] or ["DATABASE_URL"]
        deps = " ".join(_python_deps(snapshot, root)).lower()
        plan.database_scheme = (
            "postgresql+asyncpg"
            if "asyncpg" in deps
            else "postgresql+psycopg"
            if re.search(r"\bpsycopg\b(?!2)", deps) and "sqlalchemy" in app.orm
            else "postgresql"
        )
    pkg = _pkg(snapshot, root)
    scripts: dict[str, str] = pkg.get("scripts") or {}
    pm = app.package_manager or "npm"
    if "prisma" in app.orm:
        has_migrations = any(p.startswith(_rel(root, "prisma/migrations/")) for p in files)
        plan.migrate.append(
            ["npx", "prisma", "migrate", "deploy"] if has_migrations else ["npx", "prisma", "db", "push"]
        )
        if (pkg.get("prisma") or {}).get("seed"):
            plan.seed.append(["npx", "prisma", "db", "seed"])
    if "drizzle" in app.orm:
        plan.migrate.append(["npx", "drizzle-kit", "migrate"])
    if _rel(root, "alembic.ini") in snapshot.files:
        plan.migrate.append(["python", "-m", "alembic", "upgrade", "head"])
    for name in ("db:migrate", "migrate"):
        if name in scripts and not plan.migrate:
            plan.migrate.append([pm, "run", name])
    for name in ("db:seed", "seed"):
        if name in scripts and not plan.seed:
            plan.seed.append([pm, "run", name])
    if app.language == "python" and not plan.seed:
        for cand in ("app/seed.py", "seed.py", "scripts/seed.py", "src/seed.py"):
            if _rel(root, cand) in snapshot.files:
                module = cand.removesuffix(".py").replace("/", ".")
                plan.seed.append(["python", "-m", module] if "/" in cand else ["python", cand])
                break
    if (plan.migrate or plan.seed) and "postgres" not in plan.services:
        plan.warnings.append("migrations/seed found but no database was detected; they run against the app's default")


def _stubs_and_env(plan: BuildPlan, snapshot: CodeSnapshot, files: list[str], secret_names: set[str]) -> None:
    refs = _env_refs(snapshot, files)
    provided = set(plan.env) | set(plan.database_env) | {"REDIS_URL"} | PLATFORM_ENV
    for name, optional in sorted(refs.items()):
        if name in provided or name.startswith("OTEL_"):
            continue
        if STUB_ENV.match(name):
            stub = re.sub(r"_(API_)?(URL|BASE_URL|BASE|ENDPOINT|HOST)$", "", name).lower().replace("_", "-") or "ext"
            plan.stubs.append(Stub(name=stub, env=name))
        elif not optional:
            if f"SANDBOX_{name}" not in secret_names:
                plan.placeholders.append(name)
    hosts = sorted(
        {
            m.group(1)
            for p in files
            if p.endswith((".py", ".js", ".ts", ".tsx", ".mjs"))
            for m in EXTERNAL_HOST.finditer(snapshot.read(p))
        }
    )
    if hosts:
        plan.warnings.append(
            "hard-coded external hosts are unreachable in the sandbox: "
            + ", ".join(hosts[:8])
            + " — read their base URL from an environment variable so it can be stubbed"
        )
    for key, value in plan.env.items():
        if CONTROL_CHARS.search(value):
            plan.env[key] = CONTROL_CHARS.sub("", value)
