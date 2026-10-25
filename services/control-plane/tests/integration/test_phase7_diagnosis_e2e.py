"""Phase 7 end to end: a seeded missing-index defect in the Postgres-backed reference app is
run in a sandbox, detection raises an SLO finding, and diagnosis — evidence pack, rule-based
hypotheses, citation checks, a concurrency-contrast experiment (two system runs in the same
sandbox) and the rubric — ranks the true root cause first with verified citations only.
Skipped without k6."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.conftest import auth, sql
from tests.integration.test_phase3_e2e import K6, live_api  # noqa: F401
from tests.support.defects import CASES, run_case

pytestmark = pytest.mark.integration


async def test_missing_index_is_diagnosed_with_verified_evidence(live_api: str, tmp_path: Path) -> None:  # noqa: F811
    case = next(c for c in CASES if c.defect == "missing_index")
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        out = await run_case(client, live_api, tmp_path, case, K6)
        assert out.finding is not None, out.notes
        diag = out.diagnosis
        assert diag is not None and diag["status"] == "completed", diag
        top = diag["hypotheses"][0]
        assert out.rank_hit == 1, out.to_dict()
        assert top["category"] == "missing_index" and top["confidence"] in ("medium", "high"), (
            top["confidence_factors"].get("experiment"),
            [e["summary"] for e in top["evidence"] if e["kind"] == "experiment"],
        )
        assert top["confidence_factors"]["experiment"]["name"] == "concurrency_contrast"
        assert top["confidence_factors"]["experiment"]["result"] == "supports"
        kinds = {e["kind"] for e in top["evidence"] if e["relation"] == "supports" and e["check"] == "verified"}
        assert {"db_stat", "code_location", "metric_window"} <= kinds
        assert diag["disclaimer"].startswith("Hypotheses are ranked explanations")

        # Every stored hypothesis has at least one verified supporting citation (ADR-0015 confirmation).
        orphans = sql("""SELECT count(*) FROM hypotheses h WHERE NOT EXISTS (
                           SELECT 1 FROM hypothesis_evidence e WHERE e.hypothesis_id = h.id
                           AND e.relation = 'supports' AND e.check_result = 'verified')""")[0][0]
        assert orphans == 0
        # Experiment runs never become findings and don't count as baselines.
        exp_runs = sql("SELECT count(*) FROM runs WHERE config_snapshot ? 'experiment'")[0][0]
        assert exp_runs == 2
        status = sql("SELECT status FROM findings WHERE id = %s", out.finding["id"])[0][0]
        assert status == "diagnosed"

        # Feedback feeds online precision.
        email = sql(
            "SELECT u.email FROM users u JOIN memberships m ON m.user_id = u.id JOIN findings f "
            "ON f.org_id = m.org_id WHERE f.id = %s",
            out.finding["id"],
        )[0][0]
        h = auth(email)
        fb = await client.post(
            f"/v1/hypotheses/{top['id']}/feedback", headers=h, json={"verdict": "confirmed", "note": "index fixed it"}
        )
        assert fb.status_code == 200
        org_id = sql("SELECT org_id FROM findings WHERE id = %s", out.finding["id"])[0][0]
        quality = (await client.get(f"/v1/orgs/{org_id}/diagnosis-quality", headers=h)).json()
        assert quality["precision"] == 1.0 and quality["judged"] == 1
