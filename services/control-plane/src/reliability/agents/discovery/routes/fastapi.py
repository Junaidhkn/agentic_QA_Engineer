"""FastAPI adapter: decorators on ``FastAPI``/``APIRouter`` instances, router prefixes and
``include_router`` prefixes, request-body fields from Pydantic models (Python ``ast``)."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath

from reliability.agents.discovery.routes import AUTH_HINTS, METHODS, WRITE_HINTS, Route, join_paths, normalize
from reliability.agents.discovery.snapshot import CodeSnapshot

TYPE_HINTS = {
    "str": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "EmailStr": "email",
    "UUID": "str",
    "Decimal": "float",
    "datetime": "str",
    "date": "str",
}


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _annotation(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):  # Optional[X], list[X], Annotated[X, ...]
        base = _annotation(node.value)
        if base in ("Optional", "Annotated"):
            inner = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
            return _annotation(inner)
        return base
    if isinstance(node, ast.BinOp):  # X | None
        return _annotation(node.left)
    return None


class _Module:
    def __init__(self, path: str, tree: ast.Module, source: str) -> None:
        self.path, self.tree, self.source = path, tree, source
        self.routers: dict[str, str] = {}  # variable -> own prefix
        self.models: dict[str, dict[str, str]] = {}
        self.includes: list[tuple[str, str]] = []  # (router expression, prefix)
        self.imports: dict[str, str] = {}  # local name -> dotted module


def _parse(path: str, source: str) -> _Module | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    mod = _Module(path, tree, source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = _annotation(node.value.func)
            if fn in ("FastAPI", "APIRouter"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        mod.routers[target.id] = _str(_kw(node.value, "prefix")) or ""
        elif isinstance(node, ast.ClassDef):
            bases = {_annotation(b) for b in node.bases}
            if bases & {"BaseModel", "SQLModel"} or any(b and b in mod.models for b in bases):
                fields: dict[str, str] = {}
                for b in bases:
                    fields.update(mod.models.get(b or "", {}))
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        required = stmt.value is None or (
                            isinstance(stmt.value, ast.Call)
                            and _annotation(stmt.value.func) == "Field"
                            and stmt.value.args
                            and isinstance(stmt.value.args[0], ast.Constant)
                            and stmt.value.args[0].value is Ellipsis
                        )
                        if required:
                            fields[stmt.target.id] = TYPE_HINTS.get(_annotation(stmt.annotation) or "", "str")
                mod.models[node.name] = fields
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
            if node.args:
                mod.includes.append((ast.unparse(node.args[0]), _str(_kw(node, "prefix")) or ""))
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                mod.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return mod


def _include_prefixes(modules: list[_Module]) -> dict[tuple[str, str], str]:
    """(file, router variable) -> prefix added by ``include_router`` in another module."""
    by_stem: dict[str, list[_Module]] = {}
    for m in modules:
        by_stem.setdefault(PurePosixPath(m.path).stem, []).append(m)
    prefixes: dict[tuple[str, str], str] = {}
    for m in modules:
        for expr, prefix in m.includes:
            parts = expr.split(".")
            if len(parts) == 2:  # orders.router
                module_name, var = parts
            else:  # router imported by name: from .routers.orders import router as orders_router
                target = m.imports.get(parts[0], "")
                module_name, var = (target.rsplit(".", 2)[-2], target.rsplit(".", 1)[-1]) if target else ("", parts[0])
            for candidate in by_stem.get(module_name, []):
                if var in candidate.routers:
                    prefixes[(candidate.path, var)] = prefix
    return prefixes


def extract(snapshot: CodeSnapshot) -> list[Route]:
    modules = [m for p in snapshot.glob(".py") if (m := _parse(p, snapshot.read(p))) is not None]
    prefixes = _include_prefixes(modules)
    routes: list[Route] = []
    for mod in modules:
        all_models = {k: v for m in modules for k, v in m.models.items()}
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
                if owner not in mod.routers:
                    continue
                verb = dec.func.attr.upper()
                if verb == "API_ROUTE":
                    methods_node = _kw(dec, "methods")
                    verbs = [_str(e) or "" for e in getattr(methods_node, "elts", [])] or ["GET"]
                elif verb in METHODS:
                    verbs = [verb]
                else:
                    continue
                path = _str(dec.args[0]) if dec.args else _str(_kw(dec, "path"))
                if path is None:
                    continue
                full = normalize(join_paths(prefixes.get((mod.path, owner), ""), mod.routers[owner], path))
                segment = ast.get_source_segment(mod.source, node) or ""
                body: dict[str, str] = {}
                for arg in node.args.args:
                    ann = _annotation(arg.annotation)
                    if ann in all_models:
                        body = all_models[ann]
                for v in verbs:
                    v = v.upper()
                    routes.append(
                        Route(
                            method=v,
                            path=full,
                            kind="api",
                            framework="fastapi",
                            handler_file=mod.path,
                            line=node.lineno,
                            auth_hint=bool(AUTH_HINTS.search(segment)),
                            data_write=v != "GET" and bool(WRITE_HINTS.search(segment)),
                            body_fields=body if v in ("POST", "PUT", "PATCH") else {},
                            response_id_field="id" if v == "POST" else None,
                        )
                    )
    return routes
