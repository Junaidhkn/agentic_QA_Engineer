"""CI verdict contract (FR-32): a CI key with ``runs:read`` polls ``/v1/runs/{id}/verdict`` until
``finished`` and gates on ``passed``. The field set is the published contract (additive only)."""

from __future__ import annotations

import json
import uuid

import httpx

from tests.conftest import OrgFixture, auth, sql

CONTRACT = {"run_id", "status", "finished", "passed", "reason", "failed_thresholds", "summary", "new_findings", "url"}


async def _key(api: httpx.AsyncClient, org: OrgFixture, scopes: list[str]) -> dict[str, str]:
    created = await api.post(
        f"/v1/orgs/{org.org_id}/api-keys", json={"name": "ci", "scopes": scopes}, headers=auth(org.email)
    )
    assert created.status_code == 201, created.text
    return {"Authorization": f"Bearer {created.json()['secret']}"}


async def test_verdict_contract_for_ci(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    key = await _key(api, org_a, ["runs:read"])
    run_id = uuid.uuid4()
    sql(
        "INSERT INTO runs (id, org_id, project_id, kind, environment_kind, status, requested_via, config_snapshot, "
        "quota_reservation) VALUES (%s,%s,%s,'load','target_url','running','api_key','{}','{}')",
        run_id,
        org_a.org_id,
        org_a.project_id,
    )
    pending = await api.get(f"/v1/runs/{run_id}/verdict", headers=key)
    assert pending.status_code == 200 and set(pending.json()) == CONTRACT
    assert pending.json()["finished"] is False and pending.json()["passed"] is None

    summary = {
        "failed_thresholds": [{"metric": "p95_ms", "scope": "overall", "observed": 812.0, "threshold": 500}],
        "steady_state": {"p95_ms": 812.0, "error_rate": 0.002, "rps": 140.2},
    }
    sql(
        "UPDATE runs SET status = 'failed', status_reason = 'thresholds failed', summary = %s WHERE id = %s",
        json.dumps(summary),
        run_id,
    )
    sql(
        "INSERT INTO findings (id, org_id, project_id, type, severity, fingerprint, title, status, first_run_id, "
        "last_run_id, headline) VALUES (%s,%s,%s,'slo_breach','high','fp-ci','overall p95 812 ms exceeds 500 ms',"
        "'open',%s,%s,'{}')",
        uuid.uuid4(),
        org_a.org_id,
        org_a.project_id,
        run_id,
        run_id,
    )
    done = (await api.get(f"/v1/runs/{run_id}/verdict", headers=key)).json()
    assert done["finished"] is True and done["passed"] is False and done["status"] == "failed"
    assert done["failed_thresholds"][0]["metric"] == "p95_ms" and done["summary"]["p95_ms"] == 812.0
    assert [f["severity"] for f in done["new_findings"]] == ["high"]
    assert done["url"].endswith(f"/projects/{org_a.project_id}/runs/{run_id}")

    sql("UPDATE runs SET status = 'passed', summary = '{}' WHERE id = %s", run_id)
    assert (await api.get(f"/v1/runs/{run_id}/verdict", headers=key)).json()["passed"] is True

    # A key without runs:read cannot read verdicts; another org's key cannot see the run at all.
    no_scope = await _key(api, org_a, ["projects:read"])
    assert (await api.get(f"/v1/runs/{run_id}/verdict", headers=no_scope)).status_code == 403


async def test_verdict_is_tenant_scoped(api: httpx.AsyncClient, org_a: OrgFixture, org_b: OrgFixture) -> None:
    run_id = uuid.uuid4()
    sql(
        "INSERT INTO runs (id, org_id, project_id, kind, environment_kind, status, requested_via, config_snapshot, "
        "quota_reservation) VALUES (%s,%s,%s,'load','target_url','passed','api_key','{}','{}')",
        run_id,
        org_a.org_id,
        org_a.project_id,
    )
    other = await _key(api, org_b, ["runs:read"])
    assert (await api.get(f"/v1/runs/{run_id}/verdict", headers=other)).status_code == 404
