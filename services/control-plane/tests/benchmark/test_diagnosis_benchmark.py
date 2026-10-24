"""Diagnosis benchmark on the seeded-defect corpus (evaluation.md §3–4). Opt-in
(``RE_RUN_BENCHMARK=1``; the evals workflow sets it): each defect is run in a sandbox, detected,
and diagnosed. Reports detection, top-1/top-3 precision, citation validity and calibration with
denominators, writes ``benchmarks/out/diagnosis.{json,md}`` and gates top-3 ≥ 60%."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from tests.integration.test_phase3_e2e import K6, live_api  # noqa: F401
from tests.support.defects import CASES, Outcome, run_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RE_RUN_BENCHMARK") != "1", reason="set RE_RUN_BENCHMARK=1 to run the benchmark"),
]
OUT = Path(__file__).resolve().parents[4] / "benchmarks" / "out"
GATES = {"top3": 0.60, "top1": 0.45, "citation_validity": 0.90}


def summarize(outcomes: list[Outcome]) -> dict:
    detected = [o for o in outcomes if o.finding is not None]
    diagnosed = [o for o in detected if o.diagnosis is not None]
    top1 = sum(1 for o in diagnosed if o.rank_hit == 1)
    top3 = sum(1 for o in diagnosed if o.rank_hit is not None and o.rank_hit <= 3)
    proposed = sum(o.citations_proposed for o in diagnosed)
    verified = sum(o.citations_verified for o in diagnosed)
    bands: dict[str, dict[str, int]] = {}
    for o in diagnosed:
        for rank, h in enumerate((o.diagnosis or {}).get("hypotheses") or [], 1):
            b = bands.setdefault(h["confidence"], {"correct": 0, "total": 0})
            b["total"] += 1
            b["correct"] += int(rank == o.rank_hit)
    return {
        "defects": len(outcomes),
        "detected": len(detected),
        "diagnosed": len(diagnosed),
        "top1": top1 / len(diagnosed) if diagnosed else 0.0,
        "top3": top3 / len(diagnosed) if diagnosed else 0.0,
        "top1_count": top1,
        "top3_count": top3,
        "inconclusive": sum(1 for o in diagnosed if (o.diagnosis or {}).get("status") == "inconclusive"),
        "citation_validity": verified / proposed if proposed else 1.0,
        "citations": [verified, proposed],
        "calibration": {k: {**v, "accuracy": round(v["correct"] / v["total"], 3)} for k, v in bands.items()},
        "cases": [o.to_dict() for o in outcomes],
    }


async def test_diagnosis_benchmark(live_api: str, tmp_path: Path) -> None:  # noqa: F811
    outcomes = []
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        for i, case in enumerate(CASES):
            outcomes.append(await run_case(client, live_api, tmp_path / str(i), case, K6))
    report = summarize(outcomes)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "diagnosis.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [
        f"Diagnosed {report['diagnosed']} of {report['detected']} detected defects ({report['defects']} seeded).",
        f"- top-1: {report['top1_count']}/{report['diagnosed']} ({report['top1']:.0%}); "
        f"top-3: {report['top3_count']}/{report['diagnosed']} ({report['top3']:.0%})",
        f"- citation validity: {report['citations'][0]}/{report['citations'][1]} "
        f"({report['citation_validity']:.0%}); inconclusive: {report['inconclusive']}",
        "",
        "| app | defect | expected | ranked | hit |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {c['app']} | {c['defect']} | {c['expected']} | {', '.join(f'{a} ({b})' for a, b in c['ranked'])} | "
        f"{c['rank_hit'] or '—'} |"
        for c in report["cases"]
    ]
    (OUT / "diagnosis.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    assert report["top3"] >= GATES["top3"], report
    assert report["top1"] >= GATES["top1"], report
    assert report["citation_validity"] >= GATES["citation_validity"], report
    high, low = report["calibration"].get("high"), report["calibration"].get("low")
    if high and low:
        assert high["accuracy"] >= low["accuracy"], report["calibration"]  # bands must not invert
