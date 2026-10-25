"""Express adapter: ``app|router.<verb>(path, …)`` calls, ``app.use(prefix, router)`` mounts
resolved through ``require``/``import`` of router files, and ``req.body`` field usage."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from reliability.agents.discovery.routes import AUTH_HINTS, WRITE_HINTS, Route, join_paths, normalize
from reliability.agents.discovery.snapshot import CodeSnapshot

SOURCES = (".js", ".ts", ".mjs", ".cjs")
ROUTE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|all)\s*\(\s*(['\"`])([^'\"`]+)\3")
CHAIN = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*route\s*\(\s*(['\"`])([^'\"`]+)\2\s*\)((?:\s*\.\s*(?:get|post|put|patch|delete)\s*\()+)"
)
USE = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*use\s*\(\s*(['\"`])([^'\"`]+)\2\s*,\s*(?:[\w$.]+\s*,\s*)*([A-Za-z_$][\w$.]*)\s*\)"
)
REQUIRE = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)")
IMPORT = re.compile(r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]")
BODY_DESTRUCT = re.compile(r"(?:const|let|var)\s*\{([^}]+)\}\s*=\s*req\.body")
BODY_FIELD = re.compile(r"req\.body\.([A-Za-z_$][\w$]*)")
NUMERIC = re.compile(r"(qty|quantity|count|amount|price|total|age|limit|number|stock)", re.I)


def _resolve(base: str, spec: str, files: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    target = str(PurePosixPath(base).parent / spec)
    parts: list[str] = []
    for p in target.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p not in (".", ""):
            parts.append(p)
    target = "/".join(parts)
    for cand in (target, *(target + s for s in SOURCES), *(f"{target}/index{s}" for s in SOURCES)):
        if cand in files:
            return cand
    return None


def _fields(body: str) -> dict[str, str]:
    names: list[str] = []
    for m in BODY_DESTRUCT.finditer(body):
        names += [n.split(":")[0].split("=")[0].strip() for n in m.group(1).split(",")]
    names += BODY_FIELD.findall(body)
    out: dict[str, str] = {}
    for n in names:
        if not n or not re.match(r"^[A-Za-z_$][\w$]*$", n):
            continue
        out[n] = "email" if "email" in n.lower() else "int" if NUMERIC.search(n) else "str"
    return out


def _handler_text(source: str, start: int) -> str:
    """Text of the call starting at ``start`` up to its matching ')' (bounded)."""
    depth, i = 0, source.find("(", start)
    end = min(len(source), i + 8000)
    while i < end:
        c = source[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    return source[start:end]


def extract(snapshot: CodeSnapshot) -> list[Route]:
    files = {p for p in snapshot.files if p.endswith(SOURCES) and "/test" not in p and ".test." not in p}
    mounts: dict[str, list[str]] = {}  # router file -> prefixes it is mounted at
    for path in files:
        src = snapshot.read(path)
        imports = {m.group(1): m.group(2) for m in (*REQUIRE.finditer(src), *IMPORT.finditer(src))}
        for m in USE.finditer(src):
            name = m.group(4).split(".")[0]
            target = _resolve(path, imports.get(name, ""), files)
            if target:
                mounts.setdefault(target, []).append(m.group(3))

    # Propagate nested mounts (router mounted inside a mounted router).
    def prefixes(path: str, depth: int = 0) -> list[str]:
        own = mounts.get(path)
        if not own or depth > 5:
            return [""]
        parents = [p for p, children in _parents.items() if path in children]
        outer = [pp for parent in parents for pp in prefixes(parent, depth + 1)] or [""]
        return [join_paths(o, p) for o in outer for p in own]

    _parents: dict[str, set[str]] = {}
    for path in files:
        src = snapshot.read(path)
        imports = {m.group(1): m.group(2) for m in (*REQUIRE.finditer(src), *IMPORT.finditer(src))}
        for m in USE.finditer(src):
            target = _resolve(path, imports.get(m.group(4).split(".")[0], ""), files)
            if target:
                _parents.setdefault(path, set()).add(target)

    routes: list[Route] = []
    for path in sorted(files):
        src = snapshot.read(path)
        if "express" not in src and path not in mounts and "Router" not in src:
            continue
        file_auth = any(AUTH_HINTS.search(m.group(0)) for m in re.finditer(r"\.use\(\s*[A-Za-z_$][\w$.()]*\s*\)", src))
        found: list[tuple[str, str, int]] = []
        for m in ROUTE.finditer(src):
            if m.group(1) in (
                "axios",
                "http",
                "request",
                "client",
                "fetch",
                "cy",
                "page",
                "res",
                "req",
                "map",
                "cache",
            ):
                continue
            found.append((m.group(2).upper(), m.group(4), m.start()))
        for m in CHAIN.finditer(src):
            for verb in re.findall(r"\.\s*(get|post|put|patch|delete)\s*\(", m.group(4)):
                found.append((verb.upper(), m.group(3), m.start()))
        for verb, route_path, pos in found:
            if not route_path.startswith("/"):
                continue
            handler = _handler_text(src, pos)
            line = src.count("\n", 0, pos) + 1
            for prefix in prefixes(path):
                for v in ["GET", "POST"] if verb == "ALL" else [verb]:
                    routes.append(
                        Route(
                            method=v,
                            path=normalize(join_paths(prefix, route_path)),
                            kind="api",
                            framework="express",
                            handler_file=path,
                            line=line,
                            auth_hint=file_auth or bool(AUTH_HINTS.search(handler)),
                            data_write=v != "GET" and bool(WRITE_HINTS.search(handler)),
                            body_fields=_fields(handler) if v in ("POST", "PUT", "PATCH") else {},
                            response_id_field="id" if v == "POST" else None,
                        )
                    )
    return routes
