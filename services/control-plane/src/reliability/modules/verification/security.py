"""``security`` gate (low-level-design §9): a built-in scan of the lines a patch adds (secrets and
dangerous calls), plus a base-vs-patched comparison of whatever external scanners ran on the
runner (semgrep, gitleaks, osv-scanner). Only *new* high/critical findings fail the gate.
"""

from __future__ import annotations

import re
from typing import Any

from reliability.modules.verification.policy import unified_diff

RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("secret.aws_access_key", "high", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret.private_key", "high", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret.github_token", "high", re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}")),
    ("secret.slack_token", "high", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("secret.stripe_key", "high", re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{16,}")),
    (
        "secret.hardcoded_credential",
        "high",
        re.compile(r"""(?i)\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*['"][^'"\s]{12,}['"]"""),
    ),
    ("code.eval", "high", re.compile(r"(?<![\w.])(eval|exec)\s*\(")),
    ("code.shell_true", "high", re.compile(r"shell\s*=\s*True")),
    ("code.os_system", "high", re.compile(r"\bos\.(system|popen)\s*\(")),
    ("code.child_process", "high", re.compile(r"\bchild_process\b|\bexecSync\s*\(")),
    ("code.new_function", "high", re.compile(r"\bnew\s+Function\s*\(")),
    (
        "code.sql_format",
        "high",
        re.compile(r"""(execute|query|raw|text)\s*\(\s*f["']|(execute|query)\s*\([^)]*["']\s*%\s"""),
    ),
    ("code.pickle", "medium", re.compile(r"\bpickle\.loads?\s*\(")),
    ("code.yaml_load", "medium", re.compile(r"\byaml\.load\s*\((?![^)]*Loader)")),
    ("code.tls_verify_off", "medium", re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false")),
    ("code.inner_html", "medium", re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML")),
    ("config.debug_on", "medium", re.compile(r"^\s*DEBUG\s*=\s*True")),
]
BLOCKING = {"high", "critical"}


def scan_added_lines(changes: dict[str, tuple[str | None, str | None]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    path = ""
    for line in unified_diff(changes).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
            continue
        if not line.startswith("+") or not path:
            continue
        text = line[1:]
        for rule, severity, rx in RULES:
            if rx.search(text):
                findings.append({"tool": "builtin", "rule": rule, "severity": severity, "path": path})
    return findings


def _key(f: dict[str, Any]) -> tuple[str, str, str]:
    return (str(f.get("tool")), str(f.get("rule")), str(f.get("path")))


def evaluate(
    changes: dict[str, tuple[str | None, str | None]],
    base_scan: dict[str, Any] | None,
    patched_scan: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    builtin = scan_added_lines(changes)
    base_keys = {_key(f) for f in (base_scan or {}).get("findings", [])}
    new_external = [
        f
        for f in (patched_scan or {}).get("findings", [])
        if _key(f) not in base_keys and f.get("severity") in BLOCKING
    ]
    blocking = [f for f in builtin if f["severity"] in BLOCKING] + new_external
    result = {
        "builtin": builtin,
        "new_findings": new_external,
        "blocking": blocking,
        "scanners": ["builtin-diff", *((patched_scan or {}).get("tools") or [])],
        "missing_scanners": (patched_scan or {}).get("missing", ["semgrep", "gitleaks", "osv-scanner"]),
        "scanner_errors": (patched_scan or {}).get("errors", {}),
        "dast": "ZAP passive baseline not run (planned with the browser-exploration runner image)",
    }
    return not blocking, result
