"""Read-only view of the application source at the run's commit (agent-workflows §5).

Maps endpoints to handler functions through the discovery route adapters and answers the
structural questions the rules and the citation checker need: the handler's source and line
range, database calls inside loops, read-then-write sequences without locks, blocking calls,
small connection-pool settings, and whether an index exists for a column.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from reliability.agents.discovery import routes as routes_mod
from reliability.agents.discovery import stack as stack_mod
from reliability.agents.discovery.graph import path_shape
from reliability.agents.discovery.snapshot import CodeSnapshot

MAX_READ_LINES = 200
DB_CALL = re.compile(
    r"\b(db|session|s|conn|cursor|tx|prisma|knex|pool|client|repo)\s*\.\s*(execute|scalar|scalars|query|get|add|"
    r"commit|flush|find\w*|create|update|delete|select|insert|upsert|count|aggregate|raw\w*)\s*\(|"
    r"\bselect\(|\.objects\.\w+\(|\bawait\s+\w+\.(find\w*|query|count|get)\(",
)
BLOCKING = re.compile(
    r"\btime\.sleep\(|\brequests\.(get|post|put|delete)\(|\burllib\.request\.urlopen\(|\bReadFileSync\(|"
    r"\breadFileSync\(|\bexecSync\(|setTimeout\(\s*\w+\s*,\s*[^)]*\)",
)
LOCKING = re.compile(
    r"with_for_update|FOR UPDATE|select_for_update|\$transaction|isolation_level|LOCK TABLE|"
    r"\b(\w+)\s*=\s*\1\s*-|SET\s+\w+\s*=\s*\w+\s*-\s*:?\w+\s+WHERE[^\n]*>=",
    re.I,
)
POOL_SETTING = re.compile(r"\b(pool_size|max_connections|connection_limit|poolSize|max)\s*[=:]\s*(\d+)")
GENERIC_RAISE = re.compile(
    r"\braise\s+(RuntimeError|Exception|ValueError|ConnectionError|TimeoutError)\(|"
    r"\bthrow\s+new\s+Error\("
)


@dataclass
class Handler:
    endpoint: str  # "METHOD /path/{param}"
    path: str
    start: int
    end: int
    source: str
    framework: str


@dataclass
class CodeIndex:
    snapshot: CodeSnapshot
    routes: list[routes_mod.Route] = field(default_factory=list)
    commit: str | None = None

    @classmethod
    def build(cls, snapshot: CodeSnapshot, commit: str | None = None) -> CodeIndex:
        st = stack_mod.detect(snapshot)
        return cls(snapshot=snapshot, routes=routes_mod.extract(snapshot, st), commit=commit)

    # ------------------------------------------------------------ files ----
    def exists(self, path: str) -> bool:
        return path in self.snapshot.files

    def line_count(self, path: str) -> int:
        return len(self.snapshot.read(path).splitlines())

    def read(self, path: str, start: int = 1, end: int | None = None) -> str:
        lines = self.snapshot.read(path).splitlines()
        start = max(1, start)
        end = min(len(lines), end or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))

    def search(self, pattern: str, limit: int = 40) -> list[dict[str, Any]]:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for path in sorted(self.snapshot.files):
            for n, line in enumerate(self.snapshot.read(path).splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": path, "line": n, "text": line.strip()[:200]})
                    if len(hits) >= limit:
                        return hits
        return hits

    # ---------------------------------------------------------- handlers ----
    def handler(self, endpoint: str) -> Handler | None:
        """Handler for "METHOD /path" (k6 endpoint tags use IR paths with ``{{ var }}``)."""
        method, _, path = endpoint.partition(" ")
        shape = path_shape(path)
        route = next((r for r in self.routes if r.method == method and path_shape(r.path) == shape), None)
        if route is None:
            return None
        text = self.snapshot.read(route.handler_file)
        start, end = self._span(route, text)
        lines = text.splitlines()
        return Handler(
            f"{route.method} {route.path}",
            route.handler_file,
            start,
            end,
            "\n".join(lines[start - 1 : end]),
            route.framework,
        )

    def _span(self, route: routes_mod.Route, text: str) -> tuple[int, int]:
        lines = text.splitlines()
        if route.handler_file.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return route.line, min(len(lines), route.line + 40)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.lineno == route.line:
                    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    return first, node.end_lineno or node.lineno
            return route.line, min(len(lines), route.line + 40)
        # JS/TS: from the route line to the matching closing parenthesis/brace of the call.
        depth, started = 0, False
        for i in range(route.line - 1, min(len(lines), route.line + 150)):
            for ch in lines[i]:
                if ch in "({":
                    depth, started = depth + 1, True
                elif ch in ")}":
                    depth -= 1
            if started and depth <= 0:
                return route.line, i + 1
        return route.line, min(len(lines), route.line + 60)

    # --------------------------------------------------------- analysis ----
    def db_calls_in_loops(self, h: Handler) -> list[int]:
        """Absolute line numbers of DB calls that execute once per item: in a loop body or a
        comprehension's element expression (not in the iterable, which runs once)."""
        hits: set[int] = set()
        if h.path.endswith(".py"):
            try:
                tree = ast.parse(h.source)
            except SyntaxError:
                return []
            for node in ast.walk(tree):
                parts: list[ast.AST] = []
                if isinstance(node, ast.For | ast.AsyncFor | ast.While):
                    parts = list(node.body)
                elif isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
                    parts = [node.elt, *(c for g in node.generators for c in g.ifs)]
                elif isinstance(node, ast.DictComp):
                    parts = [node.key, node.value]
                for part in parts:
                    seg = ast.get_source_segment(h.source, part) or ""
                    for k, line in enumerate(seg.splitlines()):
                        if DB_CALL.search(line):
                            hits.add(h.start + getattr(part, "lineno", 1) - 1 + k)
            return sorted(hits)
        lines = h.source.splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"\bfor\s*\(|\.(map|forEach|flatMap)\s*\(|\bwhile\s*\(", line)
            if not m:
                continue
            body_start = line[m.end() :]
            for j, text in enumerate([body_start, *lines[i + 1 : i + 8]]):
                if DB_CALL.search(text):
                    hits.add(h.start + i + j)
        return sorted(hits)

    def blocking_calls(self, h: Handler) -> list[int]:
        return [h.start + i for i, line in enumerate(h.source.splitlines()) if BLOCKING.search(line)]

    def generic_raises(self, h: Handler) -> list[int]:
        return [h.start + i for i, line in enumerate(h.source.splitlines()) if GENERIC_RAISE.search(line)]

    def unlocked_read_modify_write(self, h: Handler) -> list[int]:
        """Lines where a value read earlier is written back without a lock or atomic update."""
        lines = h.source.splitlines()
        reads = [i for i, ln in enumerate(lines) if re.search(r"SELECT|\.query\(|\.get\(|\.scalar|find\w*\(", ln, re.I)]
        writes = [i for i, ln in enumerate(lines) if re.search(r"UPDATE\s|\.update\(|\.save\(|\.\w+\s*[-+]=", ln, re.I)]
        if not reads or not writes:
            return []
        guarded = {i for i, ln in enumerate(lines) if LOCKING.search(ln) and "with" in ln and "cursor" in ln}
        if any(LOCKING.search(ln) for ln in lines if "with_for_update" in ln or "FOR UPDATE" in ln.upper()):
            return []
        first_read = reads[0]
        racy = [w for w in writes if w > first_read and w not in guarded]
        # A transaction/lock scope wrapping both the read and the write makes it safe.
        for i, ln in enumerate(lines):
            if re.search(r"with\s+db\.cursor\(\)|with\s+.*lock|\$transaction|BEGIN", ln) and i < first_read:
                indent = len(ln) - len(ln.lstrip())
                inside = all((len(lines[w]) - len(lines[w].lstrip())) > indent for w in racy)
                if inside:
                    return []
        return [h.start + w for w in racy]

    def pool_settings(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.snapshot.files):
            if not path.endswith((".py", ".js", ".ts", ".mjs", ".prisma", ".env.example")):
                continue
            text = self.snapshot.read(path)
            if not re.search(r"create_engine|createPool|new Pool|PrismaClient|connection_limit|knex\(", text):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for m in POOL_SETTING.finditer(line):
                    out.append(
                        {
                            "path": path,
                            "line": n,
                            "setting": m.group(1),
                            "value": int(m.group(2)),
                            "text": line.strip()[:200],
                        }
                    )
        return out

    def has_index(self, table: str, column: str) -> bool:
        """Index on ``table.column`` declared in migrations, models or SQL."""
        col = re.escape(column)
        patterns = [
            rf"create_index\([^)]*['\"]{re.escape(table)}['\"][^)]*['\"]{col}['\"]",
            rf"CREATE\s+(UNIQUE\s+)?INDEX[^;]*\b{re.escape(table)}\b[^;]*\(\s*\"?{col}\"?",
            rf"\b{col}\b[^\n]*index\s*=\s*True",
            rf"@@index\(\[[^\]]*\b{col}\b",
            rf"\.index\(\s*\[?['\"]{col}['\"]",
            rf"\b{col}\b[^\n]*(primary_key\s*=\s*True|unique\s*=\s*True|@id|@unique)",
        ]
        rx = re.compile("|".join(patterns), re.I)
        return any(
            rx.search(self.snapshot.read(p))
            for p in self.snapshot.files
            if p.endswith((".py", ".sql", ".prisma", ".ts", ".js"))
        )

    def filter_columns(self, h: Handler) -> list[tuple[str, str, int]]:
        """(table, column, absolute line) a handler filters on: ``Model.col ==`` (table from the ORM
        model), ``FROM t WHERE col =`` (raw SQL). Primary-key lookups are skipped."""
        models = self.model_tables()
        out = []
        for i, line in enumerate(h.source.splitlines()):
            for m in re.finditer(r"\b([A-Z]\w*)\.(\w+)\s*==", line):
                table = models.get(m.group(1))
                if table and m.group(2) != "id":
                    out.append((table, m.group(2), h.start + i))
            for m in re.finditer(r"FROM\s+\"?(\w+)\"?\s+(?:\w+\s+)?WHERE\s+(?:\w+\.)?(\w+)\s*=", line, re.I):
                if m.group(2).lower() != "id":
                    out.append((m.group(1).lower(), m.group(2), h.start + i))
        return out

    def model_tables(self) -> dict[str, str]:
        """ORM class name → table name (SQLAlchemy ``__tablename__``, Prisma ``model``)."""
        out: dict[str, str] = {}
        for path in self.snapshot.files:
            if path.endswith(".py"):
                for m in re.finditer(
                    r"class\s+(\w+)\([^)]*\):\s*\n\s+__tablename__\s*=\s*['\"](\w+)['\"]", self.snapshot.read(path)
                ):
                    out[m.group(1)] = m.group(2)
            elif path.endswith(".prisma"):
                for m in re.finditer(r"model\s+(\w+)\s*\{", self.snapshot.read(path)):
                    out[m.group(1)] = m.group(1).lower()
        return out

    def tables_used(self, h: Handler) -> set[str]:
        models = self.model_tables()
        used = {t for cls, t in models.items() if re.search(rf"\b{cls}\b", h.source)}
        used |= {m.group(1).lower() for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+\"?(\w+)", h.source, re.I)}
        return used
