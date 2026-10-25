"""Reports (FR-28, UF-7): a generated report contains the right history, trends, findings and
repairs for its period (system runs excluded), exports as JSON and self-contained HTML, and can
be shared through an expiring, revocable link served with a locked-down CSP."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from reliability.platform.outbox import drain_in_process
from tests.conftest import OrgFixture, auth, sql


def _run(org: OrgFixture, kind: str, status: str, at: datetime, extra: dict | None = None) -> uuid.UUID:
    rid = uuid.uuid4()
    sql(
        "INSERT INTO runs (id, org_id, project_id, kind, environment_kind, status, requested_via, config_snapshot, "
        "quota_reservation, created_at, finished_at) VALUES (%s,%s,%s,%s,'target_url',%s,'ui',%s,'{}',%s,%s)",
        rid,
        org.org_id,
        org.project_id,
        kind,
        status,
        json.dumps(extra or {}),
        at,
        at + timedelta(minutes=5),
    )
    return rid


def _metric(org: OrgFixture, run_id: uuid.UUID, scope: str, p95: float, err: float) -> None:
    sql(
        "INSERT INTO run_metric_summaries (run_id, org_id, scope, time_window, requests, rps, error_rate, p95_ms) "
        "VALUES (%s,%s,%s,'steady_state',1000,50,%s,%s)",
        run_id,
        org.org_id,
        scope,
        err,
        p95,
    )


async def test_report_generation_share_and_revoke(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    h = auth(org_a.email)
    now = datetime.now(UTC)
    runs = []
    for i, p95 in enumerate((120.0, 95.0, 60.0)):
        rid = _run(org_a, "load", "passed" if p95 < 100 else "failed", now - timedelta(days=3 - i))
        _metric(org_a, rid, "overall", p95, 0.01)
        _metric(org_a, rid, "endpoint:GET /api/tasks", p95 * 1.5, 0.0)
        runs.append(rid)
    old = _run(org_a, "load", "passed", now - timedelta(days=90))  # outside the period
    system = _run(org_a, "load", "passed", now - timedelta(days=1), {"verification": {"gate": "load_comparison"}})
    _metric(org_a, system, "overall", 5.0, 0.0)
    _run(org_a, "functional", "passed", now - timedelta(days=2))
    sql(
        "INSERT INTO findings (id, org_id, project_id, type, severity, fingerprint, title, status, first_run_id, "
        "last_run_id, headline) VALUES (%s,%s,%s,'slo_breach','high','fp1',%s,'open',%s,%s,'{}')",
        uuid.uuid4(),
        org_a.org_id,
        org_a.project_id,
        "<script>alert(1)</script> p95 breach",
        runs[0],
        runs[0],
    )

    created = await api.post(
        f"/v1/projects/{org_a.project_id}/reports",
        headers={**h, "Idempotency-Key": "report-0001"},
        json={"from": (now - timedelta(days=30)).isoformat(), "to": (now + timedelta(minutes=1)).isoformat()},
    )
    assert created.status_code == 202, created.text
    rid = created.json()["id"]
    assert created.json()["status"] == "queued"
    await drain_in_process()
    report = (await api.get(f"/v1/reports/{rid}", headers=h)).json()
    assert report["status"] == "ready" and report["html_url"] and report["json_url"] and not report["shared"]

    doc = json.loads(_artifact(rid, "report_json"))
    assert doc["schema"] == "report@1" and doc["sections"] == ["runs", "trends", "coverage", "findings", "repairs"]
    ids = {r["id"] for r in doc["runs"]}
    assert {str(r) for r in runs} <= ids and str(old) not in ids and str(system) not in ids
    assert doc["summary"]["runs"] == {"functional": {"passed": 1}, "load": {"failed": 1, "passed": 2}}
    assert [p["p95_ms"] for p in doc["trends"]["overall"]] == [120.0, 95.0, 60.0]
    assert list(doc["trends"]) == ["overall", "endpoint:GET /api/tasks"]
    assert doc["findings"][0]["severity"] == "high" and doc["summary"]["open_findings"] == {"high": 1}

    page = _artifact(rid, "report_html").decode()
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<polyline" in page and "Run history" in page

    shared = await api.post(f"/v1/reports/{rid}/share", headers=h, json={"expires_in_days": 3})
    assert shared.status_code == 200, shared.text
    token = shared.json()["url"].rsplit("/", 1)[1]
    public = await api.get(f"/shared/reports/{token}")
    assert public.status_code == 200 and public.text == page
    assert "default-src 'none'" in public.headers["content-security-policy"]
    assert public.headers["x-robots-tag"].startswith("noindex") and public.headers["referrer-policy"] == "no-referrer"
    assert (await api.get("/shared/reports/not-a-real-token")).status_code == 404
    assert sql("SELECT count(*) FROM reports WHERE share_token_hash IS NOT NULL")[0][0] == 1
    assert token.encode() not in bytes(sql("SELECT share_token_hash FROM reports")[0][0])  # stored hashed

    assert (await api.delete(f"/v1/reports/{rid}/share", headers=h)).status_code == 204
    assert (await api.get(f"/shared/reports/{token}")).status_code == 404

    # Expired links stop working too.
    shared = await api.post(f"/v1/reports/{rid}/share", headers=h, json={"expires_in_days": 1})
    token = shared.json()["url"].rsplit("/", 1)[1]
    sql("UPDATE reports SET share_expires_at = now() - interval '1 minute' WHERE id = %s", rid)
    assert (await api.get(f"/shared/reports/{token}")).status_code == 404

    listed = (await api.get(f"/v1/projects/{org_a.project_id}/reports", headers=h)).json()
    assert [r["id"] for r in listed] == [rid]
    bad = await api.post(
        f"/v1/projects/{org_a.project_id}/reports",
        headers={**h, "Idempotency-Key": "report-0002"},
        json={"sections": ["everything"]},
    )
    assert bad.status_code == 422


async def test_reports_are_tenant_isolated(api: httpx.AsyncClient, org_a: OrgFixture, org_b: OrgFixture) -> None:
    created = await api.post(
        f"/v1/projects/{org_a.project_id}/reports",
        headers={**auth(org_a.email), "Idempotency-Key": "report-0003"},
        json={},
    )
    rid = created.json()["id"]
    await drain_in_process()
    assert (await api.get(f"/v1/reports/{rid}", headers=auth(org_b.email))).status_code == 404
    assert (await api.post(f"/v1/reports/{rid}/share", headers=auth(org_b.email), json={})).status_code == 404


def _artifact(report_id: str, kind: str) -> bytes:
    from reliability.platform.storage import get_store

    key = sql("SELECT storage_key FROM artifacts WHERE kind = %s AND storage_key LIKE %s", kind, f"%/{report_id}/%")[0][
        0
    ]
    return get_store().get_bytes(key)
