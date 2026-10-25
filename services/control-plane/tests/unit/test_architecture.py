"""Architecture fitness checks (ADR-0003, ADR-0007)."""

from __future__ import annotations

import ast
from pathlib import Path

from reliability.platform.db import SYSTEM_SESSION_ALLOWED

SRC = Path(__file__).resolve().parents[2] / "src" / "reliability"


def _module_name(path: Path) -> str:
    return "reliability." + ".".join(path.relative_to(SRC).with_suffix("").parts)


def test_system_tx_only_used_by_allowed_modules() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        uses = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "reliability.platform.db"
            and any(alias.name == "system_tx" for alias in node.names)
            for node in ast.walk(tree)
        )
        name = _module_name(path)
        if uses and not name.startswith(SYSTEM_SESSION_ALLOWED):
            offenders.append(name)
    assert not offenders, f"system_tx (BYPASSRLS) used outside allowed modules: {offenders}"


def test_no_merge_or_default_branch_push_code_paths() -> None:
    """The GitHub integration must never merge PRs or update refs on the default branch (ADR-0017)."""
    forbidden = ("/merge", "merge_pull_request", "/git/refs/heads/main", "enable_auto_merge")
    hits = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if any(f in p.read_text() for f in forbidden) and p.name != "test_architecture.py"
    ]
    assert not hits, f"forbidden GitHub write paths found in {hits}"
