"""Security scanners for the verification ``security`` gate (low-level-design §9).

Runs whichever of semgrep, gitleaks and osv-scanner are installed on the runner image
(``runners/images/security-scan``) against a source tree and normalises their JSON into
``{tool, rule, severity, path}`` findings. The control plane compares base and patched trees;
scanners never execute the customer's code.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

SCAN_TIMEOUT_S = 600
SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low", "CRITICAL": "critical", "HIGH": "high"}


def _commands(semgrep_config: str) -> dict[str, list[str]]:
    return {
        "semgrep": ["semgrep", "scan", "--config", semgrep_config, "--json", "--quiet", "--metrics=off", "."],
        "gitleaks": ["gitleaks", "detect", "--no-git", "--no-banner", "--report-format", "json", "--report-path", "-"],
        "osv-scanner": ["osv-scanner", "--format", "json", "--recursive", "."],
    }


def _semgrep(data: Any) -> list[dict[str, Any]]:
    return [
        {
            "tool": "semgrep",
            "rule": r.get("check_id", ""),
            "severity": SEVERITY.get(str((r.get("extra") or {}).get("severity", "")).upper(), "low"),
            "path": r.get("path", ""),
        }
        for r in (data or {}).get("results", [])
    ]


def _gitleaks(data: Any) -> list[dict[str, Any]]:
    return [
        {"tool": "gitleaks", "rule": f.get("RuleID", ""), "severity": "high", "path": f.get("File", "")}
        for f in (data or [])
    ]


def _osv(data: Any) -> list[dict[str, Any]]:
    out = []
    for result in (data or {}).get("results", []):
        for pkg in result.get("packages", []):
            name = (pkg.get("package") or {}).get("name", "")
            for vuln in pkg.get("vulnerabilities", []):
                sev = str((vuln.get("database_specific") or {}).get("severity", "")).upper()
                out.append(
                    {
                        "tool": "osv-scanner",
                        "rule": f"{vuln.get('id', '')}:{name}",
                        "severity": SEVERITY.get(sev, "medium"),
                        "path": (result.get("source") or {}).get("path", ""),
                    }
                )
    return out


PARSERS = {"semgrep": _semgrep, "gitleaks": _gitleaks, "osv-scanner": _osv}


async def scan_tree(root: Path, semgrep_config: str = "p/default") -> dict[str, Any]:
    """Run every installed scanner on ``root``. Missing tools are reported, not errors."""
    report: dict[str, Any] = {"tools": [], "missing": [], "errors": {}, "findings": []}
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "HTTPS_PROXY", "SSL_CERT_FILE")}
    for tool, argv in _commands(semgrep_config).items():
        exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None:
            report["missing"].append(tool)
            continue
        proc = await asyncio.create_subprocess_exec(
            exe, *argv[1:], cwd=root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), SCAN_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            report["errors"][tool] = "timed out"
            continue
        try:
            findings = PARSERS[tool](json.loads(out or b"null"))
        except (ValueError, AttributeError, TypeError):
            report["errors"][tool] = (err or out).decode(errors="replace")[-300:]
            continue
        report["tools"].append(tool)
        report["findings"].extend(findings[:500])
    return report
