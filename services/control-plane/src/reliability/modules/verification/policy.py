"""Diff policy (``diff_policy`` gate, agent-workflows §6): size limits, allowed paths, a denylist
of CI/infra/secret files, lockfiles only with dependency changes, and no new dependencies
unless approved. The repair agent's plan and write checks call the same functions (through
its ports), so what the agent may touch and what verification accepts never drift apart.
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "max_files": 10,
    "max_changed_lines": 300,
    "allowed_paths": ["**"],
    "allow_new_deps": False,
}
DENY = (
    ".github/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    "Jenkinsfile",
    "**/Dockerfile*",
    "**/docker-compose*.y*ml",
    "**/compose*.y*ml",
    "infra/**",
    "deploy/**",
    "terraform/**",
    "**/*.tf",
    "**/.env*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/.npmrc",
    "**/.pypirc",
    "**/.git/**",
)
LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "requirements.lock",
    "Gemfile.lock",
    "go.sum",
}
REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.(txt|in)$")


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """``**/`` matches any number of directories, ``*`` and ``?`` stay within one segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return _glob_regex(pattern).match(path) is not None


def normalise(path: str) -> str | None:
    """Repository-relative POSIX path, or None when it escapes the tree."""
    if not path or "\\" in path or "\x00" in path:
        return None
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or path.startswith("~"):
        return None
    return str(p)


def path_violation(path: str, constraints: dict[str, Any] | None = None) -> str | None:
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    norm = normalise(path)
    if norm is None:
        return f"{path}: path escapes the repository"
    p = PurePosixPath(norm)
    for pattern in DENY:
        if glob_match(norm, pattern):
            return f"{norm}: CI, infrastructure and secret files may not be changed ({pattern})"
    if p.name in LOCKFILES and not c["allow_new_deps"]:
        return f"{norm}: lockfiles change only when new dependencies are approved"
    if not any(glob_match(norm, g) for g in c["allowed_paths"]):
        return f"{norm}: outside the allowed paths {c['allowed_paths']}"
    return None


def _dep_names(path: str, text: str | None) -> set[str]:
    if not text:
        return set()
    name = PurePosixPath(path).name
    try:
        if REQUIREMENTS.match(name):
            return {
                re.split(r"[\s\[<>=!~;@]", ln.strip(), maxsplit=1)[0].lower()
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "-"))
            }
        if name == "package.json":
            doc = json.loads(text)
            return {
                k
                for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
                for k in (doc.get(sec) or {})
            }
        if name == "pyproject.toml":
            doc = tomllib.loads(text)
            project = doc.get("project") or {}
            reqs = list(project.get("dependencies") or [])
            for extra in (project.get("optional-dependencies") or {}).values():
                reqs += extra
            names = {re.split(r"[\s\[<>=!~;@]", r, maxsplit=1)[0].lower() for r in reqs}
            names |= {k.lower() for k in ((doc.get("tool") or {}).get("poetry") or {}).get("dependencies", {})}
            return names
    except (ValueError, tomllib.TOMLDecodeError):
        return {"<unparseable manifest>"}
    return set()


def new_dependencies(path: str, old: str | None, new: str | None) -> list[str]:
    return sorted(_dep_names(path, new) - _dep_names(path, old))


def diff_stats(changes: dict[str, tuple[str | None, str | None]]) -> dict[str, int]:
    additions = deletions = 0
    for old, new in changes.values():
        for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
    return {"files": len(changes), "additions": additions, "deletions": deletions}


def unified_diff(changes: dict[str, tuple[str | None, str | None]]) -> str:
    out: list[str] = []
    for path in sorted(changes):
        old, new = changes[path]
        out += difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
        )
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n\\ No newline at end of file\n"
    return "".join(out)


@dataclass
class PolicyResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    new_dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": self.violations,
            "stats": self.stats,
            "new_dependencies": self.new_dependencies,
        }


def check(changes: dict[str, tuple[str | None, str | None]], constraints: dict[str, Any] | None = None) -> PolicyResult:
    """``changes`` maps path -> (old content | None, new content | None)."""
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    violations = [v for path in sorted(changes) if (v := path_violation(path, c))]
    stats = diff_stats(changes)
    if stats["files"] > c["max_files"]:
        violations.append(f"{stats['files']} files changed (limit {c['max_files']})")
    if stats["additions"] + stats["deletions"] > c["max_changed_lines"]:
        violations.append(f"{stats['additions'] + stats['deletions']} lines changed (limit {c['max_changed_lines']})")
    deps = sorted({d for path, (old, new) in changes.items() for d in new_dependencies(path, old, new)})
    if deps and not c["allow_new_deps"]:
        violations.append(f"new dependencies need approval: {', '.join(deps)}")
    if not changes:
        violations.append("the patch is empty")
    return PolicyResult(not violations, violations, stats, deps)
