"""Sandbox build plans (ADR-0011): strategy selection, start commands, datastores, migrations,
seeds, stubs and placeholders on the reference apps and synthetic repositories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reliability.agents.discovery.snapshot import from_files
from reliability.modules.environments.plan import plan_for

CORPUS = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps"


def _corpus(app: str) -> dict[str, str]:
    root = CORPUS / app
    return {
        str(p.relative_to(root)): p.read_text()
        for p in root.rglob("*")
        if p.is_file() and not {"__pycache__", "node_modules"} & set(p.parts)
    }


def _plan(files: dict[str, str], secrets: set[str] | None = None) -> dict:
    snap = from_files(files)
    snap.lockfiles |= {k for k in files if k.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))}
    return plan_for(snap, secrets).to_dict()


@pytest.mark.parametrize("app", sorted(p.name for p in CORPUS.iterdir() if (p / "labels.json").exists()))
def test_every_reference_app_has_a_runnable_plan(app: str) -> None:
    plan = _plan(_corpus(app))
    labels = json.loads((CORPUS / app / "labels.json").read_text())
    assert plan["unsupported"] is None and plan["start"], plan
    assert plan["health_path"] in ("/health", "/api/health")
    want = labels.get("sandbox", {})
    assert sorted(plan["services"]) == sorted(want.get("services", []))
    assert bool(plan["migrate"]) == want.get("migrate", False) and bool(plan["seed"]) == want.get("seed", False)


def test_fastapi_tasks_plan_details() -> None:
    plan = _plan(_corpus("fastapi-tasks"))
    assert plan["start"][:4] == ["python", "-m", "uvicorn", "app.main:app"]
    assert plan["database_scheme"] == "postgresql+psycopg" and plan["database_env"] == ["DATABASE_URL"]
    assert plan["migrate"] == [["python", "-m", "alembic", "upgrade", "head"]]
    assert plan["seed"] == [["python", "-m", "app.seed"]]


def test_node_plans() -> None:
    express = _plan(_corpus("express-notes"))
    assert express["install"] == [["npm", "ci"]] and express["start"] == ["npm", "run", "start"]
    assert express["env"]["PORT"] == "{port}" and express["port"] == 3000
    nxt = _plan(_corpus("nextjs-blog"))
    assert nxt["build"] == [["npm", "run", "build"]] and nxt["env"]["NODE_ENV"] == "production"


def test_compose_strategy_reuses_database_from_compose() -> None:
    files = {
        "docker-compose.yml": (
            "services:\n  api:\n    build: .\n    ports: ['8080:3000']\n"
            "  db:\n    image: postgres:16\n    environment:\n      POSTGRES_USER: shop\n      POSTGRES_DB: shop\n"
        ),
        "package.json": json.dumps(
            {
                "dependencies": {"express": "4", "pg": "8"},
                "scripts": {"start": "node app.js", "migrate": "knex migrate"},
            }
        ),
        "app.js": (
            "const express = require('express'); const app = express();\napp.get('/health', (q, r) => r.json({}));"
        ),
    }
    plan = _plan(files)
    assert plan["strategy"] == "compose" and plan["app_service"] == "api" and plan["port"] == 3000
    assert plan["services"]["postgres"] == {"from_compose": "db", "user": "shop", "database": "shop"}
    assert plan["migrate"] == [["npm", "run", "migrate"]]


def test_dockerfile_strategy_and_prisma() -> None:
    files = {
        "Dockerfile": 'FROM node:22\nEXPOSE 4000\nCMD ["node", "server.js"]\n',
        "package.json": json.dumps(
            {
                "dependencies": {"express": "4", "@prisma/client": "5"},
                "devDependencies": {"prisma": "5"},
                "prisma": {"seed": "node seed.js"},
            }
        ),
        "prisma/schema.prisma": 'datasource db { provider = "postgresql" url = env("DATABASE_URL") }',
        "prisma/migrations/0001_init/migration.sql": "create table x();",
        "server.js": "const express = require('express'); const url = process.env.DATABASE_URL;",
    }
    plan = _plan(files)
    assert plan["strategy"] == "dockerfile" and plan["port"] == 4000
    assert "postgres" in plan["services"]
    assert plan["migrate"] == [["npx", "prisma", "migrate", "deploy"]] and plan["seed"] == [
        ["npx", "prisma", "db", "seed"]
    ]


def test_stubs_placeholders_and_hardcoded_hosts() -> None:
    files = {
        "requirements.txt": "fastapi\nuvicorn\nhttpx\n",
        "main.py": (
            "import os\nfrom fastapi import FastAPI\napp = FastAPI()\n"
            "PAY = os.environ.get('PAYMENTS_API_URL')\nKEY = os.environ['SESSION_SECRET']\n"
            "TOKEN = os.environ['MAILER_TOKEN']\nGEO = 'https://api.geo.example.com/v1'\n"
        ),
    }
    plan = _plan(files, {"SANDBOX_MAILER_TOKEN"})
    assert plan["stubs"] == [{"name": "payments", "env": "PAYMENTS_API_URL", "routes": []}]
    assert plan["placeholders"] == ["SESSION_SECRET"]  # MAILER_TOKEN comes from the SANDBOX_ secret
    assert any("api.geo.example.com" in w for w in plan["warnings"])
    assert plan["install"] == [["pip", "install", "--no-cache-dir", "-r", "requirements.txt"]]
    assert plan["start"][3] == "main:app"


def test_django_and_unsupported() -> None:
    django = _plan({"requirements.txt": "django\npsycopg2-binary\n", "manage.py": "", "app/settings.py": ""})
    assert django["start"][:3] == ["python", "manage.py", "runserver"]
    assert django["migrate"] == [["python", "manage.py", "migrate", "--noinput"]]
    assert "postgres" in django["services"]
    assert _plan({"README.md": "# docs only"})["unsupported"].startswith("no supported application")
