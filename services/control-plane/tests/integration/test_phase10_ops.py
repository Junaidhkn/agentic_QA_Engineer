"""Phase 10 operations: platform metrics, integrity invariants, the synthetic canary and
restore drills (operations §6, §7; evaluation §5)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from reliability.modules.ops import canary, checks, restore
from reliability.modules.ops.jobs import check_integrity
from reliability.platform.db import system_tx
from reliability.platform.settings import get_settings
from tests.conftest import SUPERUSER_URL, TEST_DB, OrgFixture, auth, make_org, sql

pytestmark = pytest.mark.integration
RULES = Path(__file__).resolve().parents[4] / "deploy/observability/rules/platform-slo.rules.yml"


def _scrape(text: str, name: str, **labels: str) -> float | None:
    want = ",".join(f'{k}="{v}"' for k, v in labels.items())
    prefix = f"{name}{{{want}}}" if labels else name
    for line in text.splitlines():
        if line.startswith(prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


async def test_metrics_endpoint_reports_red_queues_and_checks(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    h = auth(org_a.email)
    assert (await api.get(f"/v1/projects/{org_a.project_id}", headers=h)).status_code == 200
    assert (await api.get(f"/v1/projects/{uuid.uuid4()}", headers=h)).status_code == 404
    sql(
        "INSERT INTO execution_jobs (id, org_id, job_type, subject_id, spec, status, queued_at) "
        "VALUES (%s, %s, 'ui_explore', %s, '{}', 'queued', now() - interval '42 seconds')",
        uuid.uuid4(),
        org_a.org_id,
        uuid.uuid4(),
    )
    sql("INSERT INTO outbox (topic, payload, available_at) VALUES ('x.y', '{}', now() - interval '31 seconds')")
    async with system_tx() as s:
        await checks.record(s, checks.CANARY, True, {})
        await checks.record(s, checks.RESTORE_DRILL, False, {"error": "boom"})

    body = (await api.get("/metrics")).text
    assert _scrape(body, "re_http_requests_total", method="GET", route="/v1/projects/{project_id}", status="2xx") == 1
    assert _scrape(body, "re_http_requests_total", method="GET", route="/v1/projects/{project_id}", status="4xx") == 1
    assert 're_http_request_duration_seconds_bucket{le="0.3",method="GET",route="/v1/projects/{project_id}"}' in body
    assert _scrape(body, "re_queue_depth", queue="job:ui_explore") == 1
    assert 41 <= (_scrape(body, "re_queue_oldest_age_seconds", queue="job:ui_explore") or 0) < 120
    assert _scrape(body, "re_outbox_pending") == 1 and (_scrape(body, "re_outbox_oldest_age_seconds") or 0) >= 30
    assert _scrape(body, "re_platform_check_ok", check="canary") == 1
    assert _scrape(body, "re_platform_check_ok", check="restore_drill") == 0
    assert _scrape(body, "re_platform_check_consecutive_failures", check="restore_drill") == 1
    assert _scrape(body, "re_kill_switch_active") == 0 and _scrape(body, "re_metrics_db_up") == 1
    # Bounded labels only: no ids, no raw paths.
    assert str(org_a.project_id) not in body and str(org_a.org_id) not in body


async def test_metrics_endpoint_needs_its_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from reliability.main_api import create_app

    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("scrape-secret"))
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/metrics")).status_code == 404
        assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 404
        ok = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
        assert ok.status_code == 200 and "re_http_requests_total" in ok.text


async def test_integrity_check_finds_cross_tenant_rows_and_tampered_audit(api: httpx.AsyncClient) -> None:
    a, b = make_org("ia"), make_org("ib")
    for org in (a, b):  # a few audited actions per org
        for name in ("API_TOKEN", "TEST_PASSWORD"):
            r = await api.put(
                f"/v1/projects/{org.project_id}/secrets/{name}", headers=auth(org.email), json={"value": "v-123456"}
            )
            assert r.status_code == 204, r.text
    clean = await check_integrity()
    assert clean["ok"], clean
    assert set(clean["invariants"]) >= {
        "hypothesis_without_verified_support",
        "verified_repair_without_passed_gates",
        "pull_request_without_approval",
        "cross_tenant_runs",
        "cross_tenant_workflows",
    }
    assert clean["chains"] >= 2 and clean["audit_events_checked"] >= 2

    # A workflow owned by org B inside org A's project, and an edited audit event in org A's chain.
    sql(
        "INSERT INTO workflows (id, org_id, project_id, key, title, kind, criticality, source, status, spec) "
        "VALUES (%s, %s, %s, 'leak.check', 'x', 'api', 'normal', 'static', 'candidate', '{}')",
        uuid.uuid4(),
        b.org_id,
        a.project_id,
    )
    sql(
        "UPDATE audit_events SET action = 'target.deleted' WHERE seq = (SELECT min(seq) FROM audit_events "
        "WHERE org_id = %s)",
        a.org_id,
    )
    dirty = await check_integrity()
    assert not dirty["ok"]
    assert dirty["invariants"]["cross_tenant_workflows"] == 1
    assert [c["chain"] for c in dirty["broken_chains"]] == [str(a.org_id)]
    (status, failures) = sql("SELECT status, consecutive_failures FROM platform_checks WHERE name = 'integrity'")[0]
    assert status == "failed" and failures == 1


def _insert_run(org: OrgFixture, status: str) -> str:
    run_id = uuid.uuid4()
    sql(
        "INSERT INTO runs (id, org_id, project_id, kind, environment_kind, status, requested_via, config_snapshot, "
        "quota_reservation, queued_at) "
        "VALUES (%s, %s, %s, 'functional', 'target_url', %s, 'system', '{}', '{}', now())",
        run_id,
        org.org_id,
        org.project_id,
        status,
    )
    return str(run_id)


async def test_canary_judges_each_pair_before_starting_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    org = make_org("canary", plan="team")
    settings = get_settings()
    monkeypatch.setattr(settings, "canary_project_id", org.project_id)
    monkeypatch.setattr(settings, "canary_target_id", uuid.uuid4())
    started: list[list[str]] = []

    async def fake_start(project_id: uuid.UUID, target_id: uuid.UUID) -> list[str]:
        assert project_id == org.project_id
        pair = [_insert_run(org, "queued"), _insert_run(org, "queued")]
        started.append(pair)
        return pair

    monkeypatch.setattr(canary, "start_pair", fake_start)

    def state() -> tuple[str, int, Any]:
        row = sql("SELECT status, consecutive_failures, last_success_at FROM platform_checks WHERE name='canary'")
        return row[0]

    now = datetime.now(UTC)
    first = await canary.run(now)
    assert first["ok"] and len(started) == 1 and state()[0] == "running" and state()[2] is None

    # Still running and within the time limit: judged next time, nothing new started.
    waiting = await canary.run(now + timedelta(minutes=5))
    assert waiting["waiting"] and len(started) == 1

    for run_id in started[0]:
        sql("UPDATE runs SET status = 'passed' WHERE id = %s", uuid.UUID(run_id))
    passed = await canary.run(now + timedelta(minutes=15))
    assert passed["ok"] and len(started) == 2 and state()[0] == "ok" and state()[2] is not None

    sql("UPDATE runs SET status = 'errored' WHERE id = %s", uuid.UUID(started[1][1]))
    failed = await canary.run(now + timedelta(minutes=30))
    assert not failed["ok"] and "did not pass" in failed["reason"] and state()[:2] == ("failed", 1)

    # A pair that never finishes fails once canary_max_age_s has passed.
    stuck = await canary.run(now + timedelta(minutes=30) + timedelta(seconds=settings.canary_max_age_s + 1))
    assert not stuck["ok"] and "in time" in stuck["reason"] and state()[:2] == ("failed", 2)


async def test_canary_not_configured_is_a_failure_only_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await canary.run() == {"configured": False}
    assert not sql("SELECT 1 FROM platform_checks WHERE name = 'canary'")
    monkeypatch.setattr(get_settings(), "env", "staging")
    await canary.run()
    assert sql("SELECT status FROM platform_checks WHERE name = 'canary'") == [("failed",)]


def _test_db_url() -> str:
    return SUPERUSER_URL.rsplit("/", 1)[0] + "/" + TEST_DB


async def test_dump_restore_drill_restores_and_verifies(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    r = await api.put(
        f"/v1/projects/{org_a.project_id}/secrets/API_TOKEN", headers=auth(org_a.email), json={"value": "v-123456"}
    )
    assert r.status_code == 204
    before = {row[0] for row in sql("SELECT datname FROM pg_database")}
    result = await restore.dump_drill(_test_db_url(), SUPERUSER_URL)
    assert result.ok, result.as_detail()
    assert result.source_counts == result.restored_counts and result.source_counts["projects"] == 1
    assert result.revision["source"] == result.revision["restored"] == "0006_platform_checks"
    assert result.integrity["ok"] and result.integrity["audit_events_checked"] >= 1
    assert {"dump", "restore", "checks", "total"} <= set(result.timings_s)
    assert {row[0] for row in sql("SELECT datname FROM pg_database")} == before  # scratch database dropped
    await restore.record(result)
    (status, detail) = sql("SELECT status, detail FROM platform_checks WHERE name = 'restore_drill'")[0]
    assert status == "ok" and detail["mode"] == "dump"


def test_count_comparison_allows_only_a_trailing_shortfall() -> None:
    assert restore._compare({"runs": 100}, {"runs": 100}, 0.0) == {}
    assert restore._compare({"runs": 100}, {"runs": 99}, 0.0) == {"runs": [100, 99]}
    assert restore._compare({"runs": 100}, {"runs": 99}, 0.02) == {}
    assert restore._compare({"runs": 100}, {"runs": 101}, 0.5) == {"runs": [100, 101]}


class FakeRds:
    """Records the drill's RDS calls; the 'restored instance' is the test database itself."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self.snapshots, self.calls = snapshots, []  # type: ignore[var-annotated]

    def describe_db_snapshots(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("describe_db_snapshots", kw))
        return {"DBSnapshots": self.snapshots}

    def restore_db_instance_from_db_snapshot(self, **kw: Any) -> None:
        self.calls.append(("restore", kw))

    def describe_db_instances(self, **kw: Any) -> dict[str, Any]:
        return {"DBInstances": [{"Endpoint": {"Address": "localhost", "Port": 5432}}]}

    def delete_db_instance(self, **kw: Any) -> None:
        self.calls.append(("delete", kw))


def _cfg() -> restore.RdsDrillConfig:
    return restore.RdsDrillConfig(
        source_instance="re-prod",
        drill_instance_prefix="re-drill",
        subnet_group="staging-data",
        security_group_ids=["sg-1"],
        instance_class="db.t4g.large",
        user="postgres",
        password="",
        database=TEST_DB,
        sslmode="disable",
    )


async def test_rds_drill_restores_newest_snapshot_and_always_deletes_it() -> None:
    now = datetime.now(UTC)
    rds = FakeRds(
        [
            {"DBSnapshotIdentifier": "old", "SnapshotCreateTime": now - timedelta(days=2), "Status": "available"},
            {"DBSnapshotIdentifier": "new", "SnapshotCreateTime": now - timedelta(hours=3), "Status": "available"},
            {"DBSnapshotIdentifier": "newest", "SnapshotCreateTime": now, "Status": "creating"},
        ]
    )
    result = await restore.rds_drill(_cfg(), rds=rds, source_url=_test_db_url(), wait=lambda _: None)
    assert result.ok, result.as_detail()
    restored = next(kw for name, kw in rds.calls if name == "restore")
    assert restored["DBSnapshotIdentifier"] == "new" and restored["PubliclyAccessible"] is False
    assert restored["DBSubnetGroupName"] == "staging-data" and restored["VpcSecurityGroupIds"] == ["sg-1"]
    deleted = next(kw for name, kw in rds.calls if name == "delete")
    assert deleted["DBInstanceIdentifier"] == restored["DBInstanceIdentifier"] and deleted["SkipFinalSnapshot"]

    def broken_wait(_: str) -> None:
        raise TimeoutError("instance never became available")

    failed = await restore.rds_drill(
        _cfg(), rds=(rds2 := FakeRds(rds.snapshots)), source_url=_test_db_url(), wait=broken_wait
    )
    assert not failed.ok and "never became available" in (failed.error or "")
    assert any(name == "delete" for name, _ in rds2.calls)  # cleaned up even on failure

    none = await restore.rds_drill(_cfg(), rds=(rds3 := FakeRds([])), source_url=_test_db_url())
    assert not none.ok and "no available automated snapshot" in (none.error or "")
    assert not any(name in ("restore", "delete") for name, _ in rds3.calls)
    assert json.dumps(failed.as_detail(), default=str)


async def test_every_metric_the_alert_rules_use_is_exported(api: httpx.AsyncClient) -> None:
    """The SLO rules (deploy/observability) must not reference a metric the platform never emits."""
    import re

    rules = RULES.read_text()
    names = re.findall(r"\bre_[a-z0-9_]*[a-z0-9]\b(?!\*)", rules)
    used = {re.sub(r"_(bucket|count|sum)$", "", m) for m in names}
    await check_integrity()  # integrity gauges appear once a check has run
    async with system_tx() as s:
        await checks.record(s, checks.CANARY, True, {})
    body = (await api.get("/metrics")).text
    exported = set(re.findall(r"^# TYPE (re_[a-z0-9_]+) ", body, re.M))
    exported |= {re.sub(r"_total$", "", n) for n in exported}
    assert used and used <= exported | {n + "_total" for n in exported}, sorted(used - exported)
