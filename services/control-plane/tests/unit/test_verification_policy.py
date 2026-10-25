"""Diff policy, built-in security scan, gate applicability and archive overlays (ADR-0016)."""

from __future__ import annotations

import io
import json
import tarfile

from reliability.modules.environments.source import overlay_archive
from reliability.modules.verification import policy, security
from reliability.modules.verification.service import applicable


def test_denylisted_and_escaping_paths_are_rejected() -> None:
    for path in (
        ".github/workflows/ci.yml",
        "Dockerfile",
        "services/api/Dockerfile.prod",
        "deploy/k8s.yaml",
        "infra/main.tf",
        "app/.env",
        ".env.production",
        "../outside.py",
        "/etc/passwd",
        "docker-compose.yml",
        "package-lock.json",
    ):
        assert policy.path_violation(path) is not None, path
    assert policy.path_violation("app/routes.py") is None
    assert policy.path_violation("package-lock.json", {"allow_new_deps": True}) is None
    assert policy.path_violation("app/routes.py", {"allowed_paths": ["tests/**"]}) is not None


def test_limits_and_new_dependencies() -> None:
    small = {"app/x.py": ("a = 1\n", "a = 2\n")}
    assert policy.check(small).passed
    many = {f"app/m{i}.py": (None, "x = 1\n") for i in range(11)}
    assert any("files changed" in v for v in policy.check(many).violations)
    big = {"app/x.py": ("", "".join(f"line{i}\n" for i in range(301)))}
    assert any("lines changed" in v for v in policy.check(big).violations)
    deps = {
        "requirements.txt": ("fastapi==0.115\n", "fastapi==0.115\nrequests>=2\n"),
        "package.json": (json.dumps({"dependencies": {"a": "1"}}), json.dumps({"dependencies": {"a": "1", "b": "2"}})),
    }
    result = policy.check(deps)
    assert result.new_dependencies == ["b", "requests"] and not result.passed
    assert policy.check(deps, {"allow_new_deps": True}).passed
    assert not policy.check({}).passed  # empty patch


def test_unified_diff_and_stats() -> None:
    changes = {"app/a.py": ("x = 1\ny = 2\n", "x = 1\ny = 3\n"), "tests/test_a.py": (None, "def test(): pass\n")}
    diff = policy.unified_diff(changes)
    assert "--- a/app/a.py" in diff and "+++ b/tests/test_a.py" in diff and "--- /dev/null" in diff
    assert policy.diff_stats(changes) == {"files": 2, "additions": 2, "deletions": 1}


def test_security_scan_blocks_new_secrets_and_dangerous_calls() -> None:
    ok, _ = security.evaluate({"app/a.py": ("x = 1\n", "x = 2\n")}, None, None)
    assert ok
    for added in (
        'API_KEY = "sk_live_abcdefghijklmnop1234"\n',
        "subprocess.run(cmd, shell=True)\n",
        'cur.execute(f"SELECT * FROM t WHERE id = {uid}")\n',
        "result = eval(user_input)\n",
        "aws = 'AKIAABCDEFGHIJKLMNOP'\n",
    ):
        ok, result = security.evaluate({"app/a.py": ("", added)}, None, None)
        assert not ok and result["blocking"], added
    # Removed lines and pre-existing scanner findings never block.
    ok, _ = security.evaluate({"app/a.py": ("x = eval(y)\n", "x = int(y)\n")}, None, None)
    assert ok
    old = {"tools": ["semgrep"], "findings": [{"tool": "semgrep", "rule": "r1", "severity": "high", "path": "a.py"}]}
    new = {
        "tools": ["semgrep"],
        "findings": [*old["findings"], {"tool": "semgrep", "rule": "r2", "severity": "high", "path": "b.py"}],
    }
    ok, result = security.evaluate({"app/a.py": ("", "x = 1\n")}, old, new)
    assert not ok and [f["rule"] for f in result["new_findings"]] == ["r2"]
    assert security.evaluate({"app/a.py": ("", "x = 1\n")}, old, old)[0]


def test_gate_applicability_by_finding_type() -> None:
    base = {"regression_argv": ["python", "-m", "pytest"], "suite_id": "s"}
    perf = applicable(
        {**base, "finding": {"type": "slo_breach", "endpoint": "GET /x"}, "load": {"workflow_keys": ["w"]}}
    )
    assert perf["load_comparison"] and not perf["invariant_probe"] and not perf["existing_tests"]
    race = applicable({**base, "existing_argv": ["python", "-m", "pytest"], "finding": {"type": "invariant_violation"}})
    assert race["invariant_probe"] and not race["load_comparison"] and race["existing_tests"]


def test_overlay_archive_strips_root_and_applies_changes() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in (("repo-abc/app/main.py", b"print(1)\n"), ("repo-abc/old.txt", b"bye\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    out = overlay_archive(buf.getvalue(), {"app/main.py": "print(2)\n", "tests/test_x.py": "x\n", "old.txt": None})
    with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as tf:
        files = {m.name: tf.extractfile(m).read() for m in tf.getmembers()}  # type: ignore[union-attr]
    assert files == {"app/main.py": b"print(2)\n", "tests/test_x.py": b"x\n"}


def test_load_gate_by_finding_type() -> None:
    from reliability.modules.verification.service import load_gate
    from reliability.modules.verification.stats import Comparison

    def comp(base: float, cand: float, verdict: str, better: bool = False, worse: bool = False) -> Comparison:
        return Comparison(base, cand, 0, 0, 0, 5, verdict, worse, 0.05 if better else 0, 0, better)

    assert load_gate({"type": "slo_breach", "metric": "p95_ms"}, comp(85, 5, "improved"), comp(80, 10, "improved"))[0]
    assert not load_gate({"type": "slo_breach", "metric": "p95_ms"}, comp(85, 84, "no_change"), None)[0]
    assert not load_gate({"type": "slo_breach", "metric": "p95_ms"}, comp(85, 5, "improved", worse=True), None)[0]
    assert not load_gate(
        {"type": "slo_breach", "metric": "p95_ms"}, comp(85, 5, "improved"), comp(80, 120, "regressed")
    )[0]
    # A retry that removes 5% errors at the cost of +2 ms p95 is accepted; +200 ms is not.
    ok, cost, budget = load_gate({"type": "error_spike"}, comp(12.6, 14.8, "regressed", better=True), None)
    assert ok and cost == 2.2 and budget == 25
    assert not load_gate({"type": "error_spike"}, comp(12.6, 212.6, "regressed", better=True), None)[0]
    assert not load_gate({"type": "error_spike"}, comp(12.6, 12.0, "no_change", better=False), None)[0]
    # An SLO breach on the error rate is judged on errors too.
    assert load_gate({"type": "slo_breach", "metric": "error_rate"}, comp(12.6, 14.1, "regressed", better=True), None)[
        0
    ]
