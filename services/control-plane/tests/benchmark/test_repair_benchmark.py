"""Repair benchmark on the seeded-defect corpus (evaluation.md, plan Phase 8 gate). Opt-in
(``RE_RUN_BENCHMARK=1``; the evals workflow sets it).

Per defect: detect → diagnose → (a) the oracle agent repairs it through the real graph, sandbox
checks and verification gates — it must be *verified* (oracle pass rate); (b) a plausible but
wrong decoy patch is handed straight to verification — it must *not* be verified, ideally at the
gate it targets (false-verified rate). With a real model configured (``RE_LLM_PROVIDER=anthropic``)
the agent's own verified rate is measured instead of the oracle's. Writes
``benchmarks/out/repair.{json,md}``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from reliability.llm import Gateway, set_gateway
from reliability.llm.fakes import default_fake_client
from reliability.platform.settings import get_settings
from tests.conftest import auth, sql
from tests.integration.test_phase3_e2e import K6, live_api  # noqa: F401
from tests.support.defects import CASES, run_case, runner
from tests.support.repairs import RUNNER_TYPES, fixture, install_oracle, submit_candidate, wait_repair

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RE_RUN_BENCHMARK") != "1", reason="set RE_RUN_BENCHMARK=1 to run the benchmark"),
]
OUT = Path(__file__).resolve().parents[4] / "benchmarks" / "out"
GATES = {"oracle_pass": 0.90, "false_verified": 0.05, "agent_verified": 0.40}
REPAIRABLE = {"missing_index", "n_plus_one", "pool_exhaustion", "race_stock", "pay_errors", "slow_search"}
ONLY = {d for d in os.environ.get("RE_BENCHMARK_ONLY", "").split(",") if d}  # debugging: a subset
FAST = {
    "RE_VERIFICATION_LOAD_HOLD_S": "20",
    "RE_VERIFICATION_LOAD_SETTLE_S": "5",
    "RE_VERIFICATION_PROBE_RUNS": "3",
    "RE_VERIFICATION_BOOTSTRAP_ITERATIONS": "4000",
}


def _failed_detail(detail: dict[str, Any]) -> dict[str, Any]:
    gates = (detail.get("verification") or {}).get("gates", [])
    return {g["gate"]: g.get("result") for g in gates if g["status"] in ("failed", "errored")}


def _failed_gates(detail: dict[str, Any]) -> list[str]:
    return [
        g["gate"] for g in (detail.get("verification") or {}).get("gates", []) if g["status"] in ("failed", "errored")
    ]


async def test_repair_benchmark(live_api: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    for k, v in FAST.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    real_model = get_settings().llm_provider != "fake"
    fake = default_fake_client()
    if not real_model:
        set_gateway(Gateway(fake, get_settings()))
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
            for i, case in enumerate(c for c in CASES if c.defect in REPAIRABLE and (not ONLY or c.defect in ONLY)):
                fx = fixture(case.app, case.defect)
                row: dict[str, Any] = {"app": case.app, "defect": case.defect, "decoy_target": fx.decoy_caught_by}
                rows.append(row)
                out = await run_case(client, live_api, tmp_path / f"d{i}", case, K6)
                if out.rank_hit is None or not out.diagnosis:
                    row["skipped"] = "no correct hypothesis to repair"
                    continue
                hyp = out.diagnosis["hypotheses"][out.rank_hit - 1]
                email, user_id = sql(
                    "SELECT u.email, u.id FROM users u JOIN memberships m ON m.user_id = u.id JOIN findings f "
                    "ON f.org_id = m.org_id WHERE f.id = %s",
                    out.finding["id"],  # type: ignore[index]
                )[0]
                h = auth(email)
                cfg = runner(live_api, tmp_path / f"r{i}", RUNNER_TYPES, K6)
                if not real_model:
                    install_oracle(fake, fx)
                started = await client.post(
                    f"/v1/hypotheses/{hyp['id']}/repairs",
                    headers={**h, "Idempotency-Key": f"repair-bench-{i:04d}"},
                    json={},
                )
                assert started.status_code == 202, started.text
                done = await wait_repair(client, h, started.json()["id"], cfg, 1500)
                row["repair"] = {
                    "status": done["status"],
                    "reason": done["status_reason"],
                    "failed": _failed_gates(done),
                }
                decoy_id = await submit_candidate(hyp["id"], user_id, fx, "decoy")
                decoy = await wait_repair(client, h, decoy_id, cfg, 1500)
                failed = _failed_gates(decoy)
                row["decoy"] = {
                    "status": decoy["status"],
                    "failed": failed,
                    "caught_by_target": fx.decoy_caught_by in failed,
                }
    finally:
        set_gateway(None)
        get_settings.cache_clear()

    attempted = [r for r in rows if "repair" in r]
    verified = [r for r in attempted if r["repair"]["status"] == "awaiting_approval"]
    decoys = [r for r in rows if "decoy" in r]
    false_verified = [r for r in decoys if r["decoy"]["status"] == "awaiting_approval"]
    report = {
        "model": "real" if real_model else "oracle",
        "defects": len(rows),
        "attempted": len(attempted),
        "verified": len(verified),
        "verified_rate": len(verified) / len(attempted) if attempted else 0.0,
        "decoys": len(decoys),
        "false_verified": len(false_verified),
        "false_verified_rate": len(false_verified) / len(decoys) if decoys else 0.0,
        "decoys_caught_at_target_gate": sum(1 for r in decoys if r["decoy"]["caught_by_target"]),
        "cases": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "repair.json").write_text(json.dumps(report, indent=2, default=str))
    label = "agent (real model)" if real_model else "oracle agent"
    lines = [
        f"Repairs ({label}): {report['verified']}/{report['attempted']} verified ({report['verified_rate']:.0%}); "
        f"decoys verified: {report['false_verified']}/{report['decoys']} ({report['false_verified_rate']:.0%}); "
        f"decoys rejected at their target gate: {report['decoys_caught_at_target_gate']}/{report['decoys']}.",
        "",
        "| app | defect | repair | decoy → failed gates (target) |",
        "|---|---|---|---|",
        *(
            f"| {r['app']} | {r['defect']} | {(r.get('repair') or {}).get('status', r.get('skipped'))} | "
            f"{', '.join((r.get('decoy') or {}).get('failed', [])) or (r.get('decoy') or {}).get('status', '—')} "
            f"({r['decoy_target']}) |"
            for r in rows
        ),
    ]
    (OUT / "repair.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    assert report["false_verified_rate"] < GATES["false_verified"], report
    if real_model:
        assert report["verified_rate"] >= GATES["agent_verified"], report
    else:
        assert report["verified_rate"] >= GATES["oracle_pass"], report
