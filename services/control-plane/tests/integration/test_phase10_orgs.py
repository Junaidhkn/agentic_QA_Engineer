"""Operator org actions (runbooks: abuse report, customer data deletion): suspension stops
work and is audited; deletion removes every row and object of one org and nothing else."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import uuid

import httpx
import pytest

from reliability.modules.ops import orgs
from reliability.modules.ops.jobs import check_integrity
from reliability.platform.errors import AppError
from reliability.platform.storage import get_store
from tests.conftest import add_member, auth, make_org, sql

pytestmark = pytest.mark.integration


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"from fastapi import FastAPI\napp = FastAPI()\n"
        info = tarfile.TarInfo("app/main.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _populate(api: httpx.AsyncClient, email: str, project_id: uuid.UUID) -> str:
    h = auth(email)
    assert (
        await api.put(f"/v1/projects/{project_id}/secrets/API_TOKEN", headers=h, json={"value": "s3cret-value"})
    ).status_code == 204
    up = await api.post(
        f"/v1/projects/{project_id}/source-archives",
        content=_archive(),
        headers={**h, "Content-Type": "application/octet-stream"},
    )
    assert up.status_code == 201, up.text
    return str(up.json()["archive_artifact_id"])


async def test_suspend_then_delete_one_org(api: httpx.AsyncClient) -> None:
    gone, kept = make_org("gone"), make_org("kept")
    shared = add_member(gone, "shared@example.com", "engineer")
    sql("INSERT INTO memberships (org_id, user_id, role) VALUES (%s, %s, 'viewer')", kept.org_id, shared)
    await _populate(api, gone.email, gone.project_id)
    await _populate(api, kept.email, kept.project_id)
    sql(
        "INSERT INTO agent_threads (thread_id, org_id, graph, subject_id) VALUES (%s, %s, 'discovery', %s)",
        f"discovery:{uuid.uuid4()}",
        gone.org_id,
        uuid.uuid4(),
    )

    # Suspension: recorded, audited, and leases told to stop.
    assert await orgs.suspend(gone.org_id, "abuse report #42", "oncall") == 0
    (suspended_at, reason) = sql("SELECT suspended_at, suspended_reason FROM organizations WHERE id = %s", gone.org_id)[
        0
    ]
    assert suspended_at is not None and reason == "abuse report #42"
    assert sql("SELECT actor_type, actor_id FROM audit_events WHERE action = 'org.suspended'") == [
        ("operator", "oncall")
    ]
    await orgs.unsuspend(gone.org_id, "oncall")
    assert sql("SELECT suspended_at FROM organizations WHERE id = %s", gone.org_id) == [(None,)]

    # Dry run shows what would go; a wrong slug refuses.
    plan = (await orgs.deletion_plan(gone.org_id)).as_dict()
    assert plan["rows"]["project_secrets"] == 1 and plan["rows"]["artifacts"] == 1 and plan["rows"]["projects"] == 1
    assert plan["users_to_delete"] == 1 and plan["agent_threads"] == 1 and plan["retained"]["audit_events"] >= 2
    with pytest.raises(AppError):
        await orgs.delete(gone.org_id, "not-the-slug", "oncall")
    assert sql("SELECT count(*) FROM organizations WHERE id = %s", gone.org_id) == [(1,)]

    store = get_store()
    assert store.delete_prefix  # objects exist for both orgs before deletion
    gone_keys = [r[0] for r in sql("SELECT storage_key FROM artifacts WHERE org_id = %s", gone.org_id)]
    kept_keys = [r[0] for r in sql("SELECT storage_key FROM artifacts WHERE org_id = %s", kept.org_id)]
    assert all(store.exists(k) for k in gone_keys + kept_keys)

    result = await orgs.delete(gone.org_id, "gone-org", "oncall")
    assert result["objects"] >= 1 and result["rows"]["organizations"] == 1 and result["rows"]["users"] == 1

    # Nothing of the deleted org is left in any table with an org_id; the other org is intact.
    tables = [
        r[0]
        for r in sql(
            "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
            "USING (table_schema, table_name) WHERE c.table_schema = 'public' AND c.column_name = 'org_id' "
            "AND t.table_type = 'BASE TABLE' "
            "AND c.table_name NOT IN (SELECT inhrelid::regclass::text FROM pg_inherits)"
        )
    ]
    leftovers = {t: n for t in tables if (n := sql(f'SELECT count(*) FROM "{t}" WHERE org_id = %s', gone.org_id)[0][0])}
    # Only the append-only audit log keeps the org's events (ADR-0023).
    assert set(leftovers) == {"audit_events"} and result["retained"]["audit_events"] == leftovers["audit_events"]
    assert not any(store.exists(k) for k in gone_keys) and all(store.exists(k) for k in kept_keys)
    assert sql("SELECT count(*) FROM project_secrets WHERE org_id = %s", kept.org_id) == [(1,)]
    assert sql("SELECT count(*) FROM users WHERE email = %s", gone.email) == [(0,)]
    assert sql("SELECT count(*) FROM users WHERE id = %s", shared) == [(1,)]  # still a member elsewhere

    # The platform chain records the deletion without personal data, and still verifies.
    (meta,) = sql("SELECT metadata FROM audit_events WHERE action = 'org.deleted' AND org_id IS NULL")[0]
    assert meta["rows"]["organizations"] == 1 and gone.email not in str(meta)
    assert (await check_integrity())["ok"]


def test_org_delete_cli_is_a_dry_run_by_default() -> None:
    org = make_org("cli")
    proc = subprocess.run(
        [sys.executable, "-m", "reliability.cli", "org", "delete", str(org.org_id)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert '"slug": "cli-org"' in proc.stdout and "dry run" in proc.stdout
    assert sql("SELECT count(*) FROM organizations WHERE id = %s", org.org_id) == [(1,)]


async def test_operator_changes_an_org_plan(api: httpx.AsyncClient) -> None:
    org = make_org("upgrade", plan="startup")
    for i in range(2):
        sql("INSERT INTO projects (id, org_id, name) VALUES (%s, %s, %s)", uuid.uuid4(), org.org_id, f"p{i}")
    down = await orgs.change_plan(org.org_id, "free", "sales")
    assert down == {"org_id": str(org.org_id), "from": "startup", "to": "free", "over_limits": {"projects": [3, 1]}}
    assert sql("SELECT plan_id FROM organizations WHERE id = %s", org.org_id) == [("free",)]
    usage = (await api.get(f"/v1/orgs/{org.org_id}/usage", headers=auth(org.email))).json()
    assert usage["plan_id"] == "free" and usage["limits"]["max_vus"] == 50
    up = await orgs.change_plan(org.org_id, "team", "sales")
    assert up["over_limits"] == {} and up["from"] == "free"
    with pytest.raises(AppError):
        await orgs.change_plan(org.org_id, "platinum", "sales")
    assert sql("SELECT metadata FROM audit_events WHERE action = 'org.plan_changed' ORDER BY seq") == [
        ({"from": "startup", "to": "free"},),
        ({"from": "free", "to": "team"},),
    ]
