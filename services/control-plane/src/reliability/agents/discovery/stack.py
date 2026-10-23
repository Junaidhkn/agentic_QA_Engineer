"""Deterministic stack detection from manifests and framework markers (FR-6)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from reliability.agents.discovery.snapshot import CodeSnapshot

JS_FRAMEWORKS = {"next": "nextjs", "express": "express"}
JS_ORMS = {
    "prisma": "prisma",
    "@prisma/client": "prisma",
    "drizzle-orm": "drizzle",
    "sequelize": "sequelize",
    "typeorm": "typeorm",
    "mongoose": "mongoose",
    "knex": "knex",
}
JS_DB = {
    "pg": "postgresql",
    "postgres": "postgresql",
    "mysql2": "mysql",
    "better-sqlite3": "sqlite",
    "sqlite3": "sqlite",
    "mongodb": "mongodb",
    "redis": "redis",
    "ioredis": "redis",
}
PY_ORMS = {"sqlalchemy": "sqlalchemy", "sqlmodel": "sqlmodel", "tortoise-orm": "tortoise", "django": "django"}
PY_DB = {
    "psycopg": "postgresql",
    "psycopg2": "postgresql",
    "psycopg2-binary": "postgresql",
    "asyncpg": "postgresql",
    "pymysql": "mysql",
    "aiosqlite": "sqlite",
    "redis": "redis",
    "motor": "mongodb",
    "pymongo": "mongodb",
}


@dataclass
class App:
    root: str  # directory containing the manifest ("" = repository root)
    language: str  # javascript | python
    frameworks: list[str] = field(default_factory=list)
    orm: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    package_manager: str | None = None
    start_command: str | None = None


@dataclass
class Stack:
    apps: list[App] = field(default_factory=list)
    has_dockerfile: bool = False
    has_compose: bool = False

    @property
    def frameworks(self) -> list[str]:
        return sorted({f for a in self.apps for f in a.frameworks})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "frameworks": self.frameworks}


def _py_deps(text: str) -> set[str]:
    """Distribution names from requirements.txt lines or quoted pyproject dependency strings."""
    names = {
        m.group(1).lower()
        for m in re.finditer(r"""["']([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(?:[<>=~!;@ ]|["'])""", text)
    }
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(?:[<>=~!;@ ]|$)", line)
        if m and not line.lstrip().startswith(("#", "-", "[")) and "=" not in line.split(m.group(1), 1)[0]:
            names.add(m.group(1).lower())
    return names


def detect(snapshot: CodeSnapshot) -> Stack:
    stack = Stack(
        has_dockerfile=any(PurePosixPath(p).name == "Dockerfile" for p in snapshot.files),
        has_compose=any(
            PurePosixPath(p).name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")
            for p in snapshot.files
        ),
    )
    for path in snapshot.glob("package.json"):
        try:
            pkg = json.loads(snapshot.read(path))
        except ValueError:
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        root = str(PurePosixPath(path).parent) if "/" in path else ""
        frameworks = sorted({v for k, v in JS_FRAMEWORKS.items() if k in deps})
        if not frameworks and not root and len(snapshot.glob("package.json")) > 1:
            continue  # workspace root manifest
        lock_dir = root + "/" if root else ""
        pm = next(
            (
                m
                for f, m in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"))
                if f"{lock_dir}{f}" in snapshot.lockfiles
            ),
            "npm",
        )
        scripts = pkg.get("scripts") or {}
        stack.apps.append(
            App(
                root=root,
                language="javascript",
                frameworks=frameworks,
                orm=sorted({v for k, v in JS_ORMS.items() if k in deps}),
                databases=sorted({v for k, v in JS_DB.items() if k in deps}),
                package_manager=pm,
                start_command=f"{pm} run start" if "start" in scripts else None,
            )
        )
    for path in [*snapshot.glob("pyproject.toml"), *snapshot.glob("requirements.txt")]:
        root = str(PurePosixPath(path).parent) if "/" in path else ""
        if any(a.root == root and a.language == "python" for a in stack.apps):
            continue
        py_deps = _py_deps(snapshot.read(path))
        frameworks = sorted({f for f in ("fastapi", "flask", "django") if f in py_deps})
        stack.apps.append(
            App(
                root=root,
                language="python",
                frameworks=frameworks,
                orm=sorted({v for k, v in PY_ORMS.items() if k in py_deps}),
                databases=sorted({v for k, v in PY_DB.items() if k in py_deps}),
                package_manager="uv" if f"{root + '/' if root else ''}uv.lock" in snapshot.lockfiles else "pip",
            )
        )
    return stack
