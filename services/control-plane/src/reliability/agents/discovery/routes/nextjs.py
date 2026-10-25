"""Next.js adapter: App Router (``app/**/route.ts`` handlers, ``page.tsx`` pages, server actions)
and Pages Router (``pages/api/**`` handlers, ``pages/**`` pages)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from reliability.agents.discovery.routes import AUTH_HINTS, METHODS, WRITE_HINTS, Route, normalize
from reliability.agents.discovery.snapshot import CodeSnapshot

EXPORTED = re.compile(r"export\s+(?:async\s+)?(?:function\s+|const\s+)(GET|POST|PUT|PATCH|DELETE)\b")
REQ_METHOD = re.compile(
    r"""req\.method\s*(?:===?|!==?)\s*['"](GET|POST|PUT|PATCH|DELETE)['"]|case\s+['"](GET|POST|PUT|PATCH|DELETE)['"]"""
)
JSON_FIELDS = re.compile(r"(?:const|let)\s*\{([^}]+)\}\s*=\s*(?:await\s+(?:req|request)\.json\(\)|req\.body)")
SCRIPT = (".ts", ".tsx", ".js", ".jsx")
PRIVATE = re.compile(r"/(_[^/]*|@[^/]*)(/|$)")  # private folders and parallel-route slots


def _segments(rel: str) -> str:
    parts = [p for p in PurePosixPath(rel).parent.parts if not (p.startswith("(") and p.endswith(")"))]
    return normalize("/" + "/".join(parts))


def _fields(src: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in JSON_FIELDS.finditer(src):
        for raw in m.group(1).split(","):
            name = raw.split(":")[0].split("=")[0].strip()
            if re.match(r"^[A-Za-z_$][\w$]*$", name):
                out[name] = "email" if "email" in name.lower() else "str"
    return out


def extract(snapshot: CodeSnapshot, roots: list[str]) -> list[Route]:
    routes: list[Route] = []
    for root in roots or [""]:
        base = f"{root}/" if root else ""
        for src_dir in ("", "src/"):
            app_dir, pages_dir = f"{base}{src_dir}app/", f"{base}{src_dir}pages/"
            for path in snapshot.files:
                if not path.endswith(SCRIPT):
                    continue
                src = snapshot.read(path)
                if path.startswith(app_dir):
                    rel = path[len(app_dir) :]
                    if PRIVATE.search("/" + rel):
                        continue
                    name = PurePosixPath(rel).stem
                    url = _segments(rel)
                    if name == "route":
                        for verb in dict.fromkeys(EXPORTED.findall(src)):
                            routes.append(
                                Route(
                                    method=verb,
                                    path=url,
                                    kind="api",
                                    framework="nextjs",
                                    handler_file=path,
                                    line=src.count("\n", 0, src.find(verb)) + 1,
                                    auth_hint=bool(AUTH_HINTS.search(src)),
                                    data_write=verb != "GET" and bool(WRITE_HINTS.search(src)),
                                    body_fields=_fields(src) if verb in ("POST", "PUT", "PATCH") else {},
                                    response_id_field="id" if verb == "POST" else None,
                                )
                            )
                    elif name == "page":
                        routes.append(
                            Route(
                                method="GET",
                                path=url,
                                kind="page",
                                framework="nextjs",
                                handler_file=path,
                                line=1,
                                auth_hint=bool(AUTH_HINTS.search(src)),
                                data_write="use server" in src and bool(WRITE_HINTS.search(src)),
                            )
                        )
                elif path.startswith(pages_dir):
                    rel = path[len(pages_dir) :]
                    stem = PurePosixPath(rel).stem
                    if stem.startswith("_"):
                        continue
                    url_rel = (
                        str(PurePosixPath(rel).with_suffix("")) if stem != "index" else str(PurePosixPath(rel).parent)
                    )
                    url = normalize("/" + url_rel.replace(".", ""))
                    if rel.startswith("api/"):
                        verbs = [a or b for a, b in REQ_METHOD.findall(src)] or ["GET", "POST"]
                        for verb in dict.fromkeys(v for v in verbs if v in METHODS):
                            routes.append(
                                Route(
                                    method=verb,
                                    path=url,
                                    kind="api",
                                    framework="nextjs",
                                    handler_file=path,
                                    line=1,
                                    auth_hint=bool(AUTH_HINTS.search(src)),
                                    data_write=verb != "GET" and bool(WRITE_HINTS.search(src)),
                                    body_fields=_fields(src) if verb in ("POST", "PUT", "PATCH") else {},
                                    response_id_field="id" if verb == "POST" else None,
                                )
                            )
                    else:
                        routes.append(
                            Route(
                                method="GET",
                                path=url,
                                kind="page",
                                framework="nextjs",
                                handler_file=path,
                                line=1,
                                auth_hint=bool(AUTH_HINTS.search(src)),
                            )
                        )
    return routes
