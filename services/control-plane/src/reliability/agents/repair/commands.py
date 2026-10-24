"""The fixed command list a repair may run (agent-workflows §6: allowlisted commands only).

Commands are derived from the repository — never written by the model — and the runner
refuses anything whose executable is not on its own allowlist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from reliability.agents.discovery.snapshot import CodeSnapshot

PY_TEST = re.compile(r"(^|/)(tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py)$")
JS_TEST = re.compile(r"(^|/)(__tests__/.*|[^/]*\.(test|spec)\.[cm]?[jt]sx?)$")


@dataclass(frozen=True)
class Commands:
    language: str
    runner: str  # pytest | jest | vitest | node-test
    existing: list[str] | None  # the repository's own test command, when it has tests
    test_dir: str
    root: str = ""  # app directory inside the source tree; commands run there

    def _rel(self, paths: list[str]) -> list[str]:
        prefix = f"{self.root.rstrip('/')}/" if self.root else ""
        return [p.removeprefix(prefix) for p in paths]

    def regression(self, paths: list[str]) -> list[str]:
        paths = self._rel(paths)
        if self.runner == "pytest":
            return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]
        if self.runner == "jest":
            return ["npx", "--no-install", "jest", "--runTestsByPath", *paths]
        if self.runner == "vitest":
            return ["npx", "--no-install", "vitest", "run", *paths]
        return ["node", "--test", *paths]

    def existing_without(self, paths: list[str]) -> list[str] | None:
        if self.existing is None:
            return None
        if self.runner == "pytest":
            return [*self.existing, *(f"--ignore={p}" for p in self._rel(paths))]
        return list(self.existing)

    def is_test_path(self, path: str) -> bool:
        return bool((PY_TEST if self.language == "python" else JS_TEST).search(path))


def detect(snapshot: CodeSnapshot, root: str = "") -> Commands | None:
    prefix = f"{root.rstrip('/')}/" if root else ""
    files = [p.removeprefix(prefix) for p in snapshot.files if p.startswith(prefix)]
    if any(p.endswith(".py") for p in files) and not any(p == "package.json" for p in files):
        tests = [p for p in files if PY_TEST.search(p)]
        existing = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"] if tests else None
        return Commands("python", "pytest", existing, f"{prefix}tests", root)
    pkg_text = snapshot.files.get(f"{prefix}package.json")
    if pkg_text is None:
        return None
    try:
        pkg = json.loads(pkg_text)
    except ValueError:
        return None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    runner = "jest" if "jest" in deps else "vitest" if "vitest" in deps else "node-test"
    has_tests = any(JS_TEST.search(p) for p in files) and bool((pkg.get("scripts") or {}).get("test"))
    lang = "typescript" if "typescript" in deps else "javascript"
    return Commands(lang, runner, ["npm", "test", "--silent"] if has_tests else None, f"{prefix}test", root)
