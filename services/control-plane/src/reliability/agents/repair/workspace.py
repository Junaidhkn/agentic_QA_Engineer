"""In-memory workspace for one repair attempt: the code snapshot at the base commit plus the
agent's edits as an overlay. Nothing touches a real checkout or Git; edits become a patch
that the deterministic verification pipeline applies to a fresh copy of the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from reliability.agents.discovery.snapshot import CodeSnapshot

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MAX_SEARCH_RESULTS = 50


class PatchError(ValueError):
    pass


@dataclass
class Workspace:
    snapshot: CodeSnapshot
    overlay: dict[str, str | None] = field(default_factory=dict)

    def exists(self, path: str) -> bool:
        if path in self.overlay:
            return self.overlay[path] is not None
        return path in self.snapshot.files

    def read(self, path: str) -> str | None:
        if path in self.overlay:
            return self.overlay[path]
        return self.snapshot.files.get(path)

    def files(self) -> list[str]:
        names = set(self.snapshot.files) | {p for p, v in self.overlay.items() if v is not None}
        return sorted(n for n in names if self.exists(n))

    def list_paths(self, prefix: str = "") -> list[str]:
        return [p for p in self.files() if p.startswith(prefix)]

    def search(self, pattern: str, path_prefix: str = "") -> list[tuple[str, int, str]]:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise PatchError(f"invalid pattern: {exc}") from exc
        hits: list[tuple[str, int, str]] = []
        for path in self.list_paths(path_prefix):
            for n, line in enumerate((self.read(path) or "").splitlines(), 1):
                if rx.search(line):
                    hits.append((path, n, line.strip()[:200]))
                    if len(hits) >= MAX_SEARCH_RESULTS:
                        return hits
        return hits

    def write(self, path: str, content: str) -> None:
        self.overlay[path] = content

    def changes(self) -> dict[str, tuple[str | None, str | None]]:
        """path -> (base content | None, new content | None) for files that actually differ."""
        out = {}
        for path, new in self.overlay.items():
            old = self.snapshot.files.get(path)
            if old != new:
                out[path] = (old, new)
        return out

    def apply_patch(self, diff: str) -> list[str]:
        """Apply a unified diff (one or more files). Context lines must match exactly."""
        touched: list[str] = []
        lines = diff.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            if not lines[i].startswith("--- "):
                i += 1
                continue
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("malformed diff: '---' without '+++'")
            old_name = _name(lines[i][4:])
            new_name = _name(lines[i + 1][4:])
            path = new_name or old_name
            if path is None:
                raise PatchError("diff deletes a file without naming it")
            i += 2
            original = (self.read(old_name) if old_name else "") or ""
            if old_name and self.read(old_name) is None:
                raise PatchError(f"{old_name}: file not found")
            src = original.splitlines(keepends=True)
            out: list[str] = []
            pos = 0
            while i < len(lines) and lines[i].startswith("@@"):
                m = HUNK.match(lines[i])
                if not m:
                    raise PatchError(f"malformed hunk header: {lines[i].strip()}")
                start = max(int(m.group(1)) - 1, 0) if int(m.group(2) or 1) else int(m.group(1))
                if start < pos:
                    raise PatchError("overlapping hunks")
                out.extend(src[pos:start])
                pos = start
                i += 1
                while i < len(lines) and not lines[i].startswith(("@@", "--- ")):
                    line = lines[i]
                    tag, body = line[:1], line[1:]
                    if line.startswith("\\"):
                        if out and out[-1].endswith("\n"):
                            out[-1] = out[-1][:-1]
                    elif tag in (" ", ""):
                        if pos >= len(src) or src[pos].rstrip("\n") != body.rstrip("\n"):
                            raise PatchError(f"{path}: context does not match at line {pos + 1}")
                        out.append(src[pos])
                        pos += 1
                    elif tag == "-":
                        if pos >= len(src) or src[pos].rstrip("\n") != body.rstrip("\n"):
                            raise PatchError(f"{path}: removed line does not match at line {pos + 1}")
                        pos += 1
                    elif tag == "+":
                        out.append(body if body.endswith("\n") else body + "\n")
                    else:
                        raise PatchError(f"{path}: unexpected line in hunk: {line[:40]!r}")
                    i += 1
            out.extend(src[pos:])
            if new_name is None:
                self.overlay[old_name or path] = None
            else:
                self.overlay[new_name] = "".join(out)
            touched.append(path)
        if not touched:
            raise PatchError("no file changes found in the diff")
        return touched


def _name(header: str) -> str | None:
    name = header.split("\t", 1)[0].strip()
    if name == "/dev/null":
        return None
    if name.startswith(("a/", "b/")):
        name = name[2:]
    return name
