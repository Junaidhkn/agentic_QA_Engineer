"""Agent evaluation harness (evaluation.md §3–4).

For each reference app: discovery (route recall, page recall, candidate IR validity, model
contribution) and — when the app can be started locally and k6 is available — generated-test
validity: every API workflow is compiled and dry-run 3× (1 VU, 1 iteration) like the runner's
``test_validation`` job. Gates: route recall ≥ 0.95, validity ≥ 0.85.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from reliability.agents.common import checkpoint
from reliability.agents.common.budget import GraphBudget
from reliability.agents.discovery import graph, routes, snapshot, stack
from reliability.llm import get_gateway
from reliability.llm.gateway import NullSink
from reliability.modules.testsuites.compiler import compile_bundle
from reliability.modules.workflows.ir import validation_errors

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "reference-apps"
GATES = {"route_recall": 0.95, "test_validity": 0.85}


def _files(root: Path) -> dict[str, str]:
    skip = {"__pycache__", "node_modules", ".next"}
    return {str(p.relative_to(root)): p.read_text(errors="replace") for p in root.rglob("*")
            if p.is_file() and not skip & set(p.parts)}


class Ports:
    def __init__(self, files: dict[str, str]) -> None:
        self.files, self.candidates = files, []

    async def snapshot(self) -> tuple[str, snapshot.CodeSnapshot]:
        return "0" * 40, snapshot.from_files(self.files)

    async def openapi_documents(self) -> list[str]:
        return []

    async def secret_names(self) -> set[str]:
        return set()

    async def progress(self, step: str, detail: dict[str, Any]) -> None:
        return None

    async def persist(self, candidates: list[dict[str, Any]], stack_: dict[str, Any]) -> dict[str, int]:
        self.candidates = candidates
        return {"created": len(candidates)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start(app_dir: Path, run: dict[str, Any]) -> tuple[subprocess.Popen[bytes], str] | None:
    if run.get("requires") and not (app_dir / run["requires"]).exists():
        return None
    port = _free_port()
    fill = {"port": str(port), "python": sys.executable}
    cmd = [c.format(**fill) for c in run["cmd"]]
    if shutil.which(cmd[0]) is None and not os.path.exists(cmd[0]):
        return None
    env = {**os.environ, "DEFECTS": "", **{k: v.format(**fill) for k, v in run.get("env", {}).items()}}
    proc = subprocess.Popen(cmd, cwd=app_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(base + run.get("health", "/"), timeout=0.5).status_code < 500:
                return proc, base
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.terminate()
    return None


def _dry_run(k6: str, bundle_dir: Path, exec_name: str, base: str, repeats: int = 3) -> tuple[int, str]:
    passed, last = 0, ""
    opts = {"scenarios": {"dry": {"executor": "per-vu-iterations", "vus": 1, "iterations": 1, "exec": exec_name,
                                  "maxDuration": "60s"}},
            "thresholds": {"checks": ["rate==1"], "http_req_failed": ["rate==0"]}}
    for _ in range(repeats):
        (bundle_dir / "options.json").write_text(json.dumps(opts))
        res = subprocess.run([k6, "run", "--quiet", "--no-color", "scenarios.js"], cwd=bundle_dir,
                             env={**os.environ, "BASE_URL": base, "THINK_MIN_S": "0", "THINK_MAX_S": "0"},
                             capture_output=True, text=True, timeout=120)
        passed += res.returncode == 0
        last = (res.stdout + res.stderr)[-600:] if res.returncode else last
    return passed, last


async def evaluate(app_dir: Path, use_llm: bool, k6: str | None) -> dict[str, Any]:
    labels = json.loads((app_dir / "labels.json").read_text())
    files = _files(app_dir)
    snap = snapshot.from_files(files)
    found = routes.extract(snap, stack.detect(snap))
    api = {r.key for r in found if r.kind == "api"}
    pages = {r.path for r in found if r.kind == "page"}
    expected_api, expected_pages = set(labels["routes"]), set(labels["pages"])
    out: dict[str, Any] = {
        "app": labels["app"],
        "route_recall": len(api & expected_api) / len(expected_api) if expected_api else 1.0,
        "page_recall": len(pages & expected_pages) / len(expected_pages) if expected_pages else None,
        "missed_routes": sorted(expected_api - api),
        "extra_routes": sorted(api - expected_api),
    }
    ports = Ports(files)
    budget = GraphBudget.for_graph("discovery")
    deps = graph.DiscoveryDeps(ports, get_gateway(), NullSink(), budget, uuid.uuid4(), uuid.uuid4(), use_llm)
    result = await graph.run(deps, f"eval:{uuid.uuid4()}")
    cands = ports.candidates
    out.update(candidates=len(cands), from_model=sum(c.get("origin") == "model" for c in cands),
               invalid_ir=sum(bool(validation_errors(c["ir"])) for c in cands), llm_tokens=budget.spent,
               llm_rejected=len(result.get("llm", {}).get("rejected", [])))
    api_irs = [c["ir"] for c in cands if c["ir"]["kind"] == "api"]
    run = labels.get("run")
    started = _start(app_dir, run) if run and k6 and api_irs else None
    if started is None:
        out["test_validity"] = None
        out["validity_note"] = "skipped (app not runnable here or k6 missing)"
        return out
    proc, base = started
    try:
        bundle = compile_bundle(api_irs)
        with tempfile.TemporaryDirectory() as tmp:
            tarfile.open(fileobj=io.BytesIO(bundle.tarball("k6")), mode="r:gz").extractall(tmp, filter="data")
            names = json.loads(bundle.files["manifest.json"])["exec_names"]
            results = {}
            for key, exec_name in names.items():
                passed, log = _dry_run(k6, Path(tmp), exec_name, base)
                results[key] = {"passed": passed, "status": "valid" if passed == 3 else
                                "flaky" if passed else "invalid", "log": log or None}
    finally:
        proc.terminate()
        proc.wait(10)
    out["tests"] = results
    out["test_validity"] = sum(r["status"] == "valid" for r in results.values()) / len(results)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="include the model synthesis step")
    parser.add_argument("--out", default=str(ROOT / "out"))
    args = parser.parse_args()
    checkpoint.use_memory_checkpointer()
    k6 = os.environ.get("K6_BINARY") or shutil.which("k6")
    reports = [asyncio.run(evaluate(app, args.llm, k6)) for app in sorted(CORPUS.iterdir())
               if (app / "labels.json").exists()]
    recall = min(r["route_recall"] for r in reports)
    validities = [r["test_validity"] for r in reports if r["test_validity"] is not None]
    validity = sum(validities) / len(validities) if validities else None
    gates = {
        "route_recall": {"value": recall, "min": GATES["route_recall"], "ok": recall >= GATES["route_recall"]},
        "test_validity": {"value": validity, "min": GATES["test_validity"],
                          "ok": validity is None or validity >= GATES["test_validity"]},
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps({"apps": reports, "gates": gates, "llm": args.llm}, indent=2))
    lines = ["| app | route recall | candidates | from model | test validity |", "|---|---|---|---|---|"]
    for r in reports:
        v = "skipped" if r["test_validity"] is None else f"{r['test_validity']:.0%}"
        lines.append(f"| {r['app']} | {r['route_recall']:.0%} | {r['candidates']} | {r['from_model']} | {v} |")
    lines.append("")
    lines += [f"- {k}: {g['value'] if g['value'] is None else round(g['value'], 3)} (min {g['min']}) "
              f"{'PASS' if g['ok'] else 'FAIL'}" for k, g in gates.items()]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if all(g["ok"] for g in gates.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
