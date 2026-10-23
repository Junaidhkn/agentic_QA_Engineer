"""Framework route adapters (FR-6): deterministic extraction of a route table."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from reliability.agents.discovery.snapshot import CodeSnapshot
from reliability.agents.discovery.stack import Stack

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@dataclass
class Route:
    method: str
    path: str  # template with {param} placeholders
    kind: str  # api | page
    framework: str
    handler_file: str
    line: int
    auth_hint: bool = False
    data_write: bool = False
    body_fields: dict[str, str] = field(default_factory=dict)  # field -> type hint (str/int/float/bool/email)
    response_id_field: str | None = None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def join_paths(*parts: str) -> str:
    joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + joined if joined else "/"


def normalize(path: str) -> str:
    """Express ``:id`` and Next ``[id]`` → ``{id}``; collapse slashes; drop trailing slash."""
    path = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)\??", r"{\1}", path)
    path = re.sub(r"\[\[?\.\.\.([A-Za-z_][A-Za-z0-9_]*)\]?\]", r"{\1}", path)
    path = re.sub(r"\[([A-Za-z_][A-Za-z0-9_]*)\]", r"{\1}", path)
    path = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*):[^}]*\}", r"{\1}", path)  # FastAPI {p:path}
    path = re.sub(r"/+", "/", path)
    return path.rstrip("/") or "/"


AUTH_HINTS = re.compile(
    r"(get_current_user|current_?user|require_?auth|requireAuth|isAuthenticated|authenticate|verifyToken|"
    r"auth\(|getServerSession|getSession|withAuth|jwt\.verify|login_required|HTTPBearer|OAuth2)",
    re.I,
)
WRITE_HINTS = re.compile(
    r"(\.add\(|\.commit\(|\.insert|\.update\(|\.delete\(|\.create\(|\.save\(|\.upsert\(|\.destroy\(|"
    r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|\.execute\()",
    re.I,
)


def extract(snapshot: CodeSnapshot, stack: Stack) -> list[Route]:
    from reliability.agents.discovery.routes import express, fastapi, nextjs

    routes: list[Route] = []
    frameworks = set(stack.frameworks)
    if "fastapi" in frameworks:
        routes += fastapi.extract(snapshot)
    if "express" in frameworks:
        routes += express.extract(snapshot)
    if "nextjs" in frameworks:
        routes += nextjs.extract(snapshot, [a.root for a in stack.apps if "nextjs" in a.frameworks])
    seen: dict[str, Route] = {}
    for r in routes:
        seen.setdefault(f"{r.kind} {r.key}", r)
    return sorted(seen.values(), key=lambda r: (r.kind, r.path, METHODS.index(r.method) if r.method in METHODS else 9))
