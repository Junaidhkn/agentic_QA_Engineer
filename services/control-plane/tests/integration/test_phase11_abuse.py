"""Phase 11: abuse review queue (threat model A1). A new org's near-maximum first load test
against a new host — or verification churn — is held for an operator; owners are emailed;
clearing lets the org run, suspending stops it."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from reliability.modules.abuse import service as abuse
from reliability.modules.notifications import service as notifications
from reliability.platform.outbox import drain_in_process
from tests.conftest import OrgFixture, auth, make_org, sql
from tests.integration.test_phase3_e2e import IR

pytestmark = pytest.mark.integration


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject))


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(notifications, "get_email_sender", lambda: box)
    from reliability.modules.abuse import jobs

    monkeypatch.setattr(jobs, "get_email_sender", lambda: box)
    return box


def _target(org: OrgFixture, host: str, status: str = "verified") -> uuid.UUID:
    tid = uuid.uuid4()
    sql(
        "INSERT INTO targets (id, org_id, project_id, base_url, host, environment, verification_token, status, "
        "verified_at, verification_expires_at, created_by) VALUES (%s,%s,%s,%s,%s,'staging','tok',%s,"
        "CASE WHEN %s = 'verified' THEN now() END, CASE WHEN %s = 'verified' THEN now() + interval '1 day' END, %s)",
        tid,
        org.org_id,
        org.project_id,
        f"https://{host}",
        host,
        status,
        status,
        status,
        org.user_id,
    )
    return tid


async def _ready(api: httpx.AsyncClient, org: OrgFixture) -> dict[str, str]:
    h = auth(org.email)
    assert (await api.post(f"/v1/projects/{org.project_id}/workflows", headers=h, json={"spec": IR})).status_code == 201
    suite = await api.post(
        f"/v1/projects/{org.project_id}/test-suites", json={}, headers={**h, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert suite.status_code == 201, suite.text
    return h


async def _load(api: httpx.AsyncClient, org: OrgFixture, h: dict[str, str], target: uuid.UUID, vus: int) -> Any:
    profile = await api.post(
        f"/v1/projects/{org.project_id}/load-profiles",
        headers=h,
        json={
            "name": f"p{uuid.uuid4().hex[:6]}",
            "type": "load",
            "max_vus": vus,
            "stages": [{"duration_s": 60, "target": vus}],
        },
    )
    assert profile.status_code == 201, profile.text
    return await api.post(
        f"/v1/projects/{org.project_id}/runs",
        headers={**h, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "kind": "load",
            "load_profile_id": profile.json()["id"],
            "environment": {"type": "target_url", "target_id": str(target)},
        },
    )


async def test_new_org_max_first_load_is_reviewed_then_cleared(api: httpx.AsyncClient, mail: Outbox) -> None:
    org = make_org("fresh", plan="indie")  # max 500 VUs → held from 400
    h = await _ready(api, org)
    target = _target(org, "shop.fresh.example")

    held = await _load(api, org, h, target, 450)
    assert held.status_code == 403 and held.json()["error"]["code"] == "UNDER_REVIEW", held.text
    review_id = held.json()["error"]["details"]["review_id"]
    again = await _load(api, org, h, target, 450)
    assert again.json()["error"]["details"]["review_id"] == review_id  # one open review per org
    assert sql("SELECT count(*) FROM runs WHERE org_id = %s", org.org_id) == [(0,)]
    (reason, status, signals) = sql(
        "SELECT reason, status, signals FROM abuse_reviews WHERE id = %s", uuid.UUID(review_id)
    )[0]
    assert reason == "new_org_max_load_new_target" and status == "open"
    assert (
        signals["max_vus"] == 450
        and signals["first_load_for_target"]
        and signals["target_host"] == "shop.fresh.example"
    )

    # Small tests keep working while the review is open.
    small = await _load(api, org, h, target, 20)
    assert small.status_code == 202 and small.json()["status"] == "pending_approval", small.text

    await drain_in_process()
    assert mail.sent == [(org.email, "Your load test is waiting for a short safety review")]
    assert "re_abuse_reviews_open 1.0" in (await api.get("/metrics")).text

    review = await abuse.decide(uuid.UUID(review_id), "cleared", "oncall", "known customer, staging host")
    assert review.status == "cleared" and review.decided_by == "oncall"
    with pytest.raises(Exception):
        await abuse.decide(uuid.UUID(review_id), "suspended", "oncall", "too late")
    await drain_in_process()
    assert mail.sent[-1] == (org.email, "Safety review complete: load tests are enabled")
    sql("UPDATE runs SET status = 'cancelled' WHERE org_id = %s", org.org_id)
    allowed = await _load(api, org, h, target, 450)
    assert allowed.status_code == 202, allowed.text  # cleared orgs are not queued again
    actions = {r[0] for r in sql("SELECT action FROM audit_events WHERE org_id = %s", org.org_id)}
    assert {"abuse.review_opened", "abuse.review_cleared"} <= actions


async def test_verification_churn_is_reviewed_and_can_suspend(api: httpx.AsyncClient, mail: Outbox) -> None:
    org = make_org("hunter", plan="indie")
    sql("UPDATE organizations SET created_at = now() - interval '60 days' WHERE id = %s", org.org_id)
    h = await _ready(api, org)
    target = _target(org, "ok.hunter.example")
    for i in range(4):
        _target(org, f"probe{i}.victim.example", status="failed" if i < 3 else "pending")

    held = await _load(api, org, h, target, 10)  # small and on an old org, but hunting for hosts
    assert held.status_code == 403 and held.json()["error"]["code"] == "UNDER_REVIEW"
    review_id = uuid.UUID(held.json()["error"]["details"]["review_id"])
    assert sql("SELECT reason FROM abuse_reviews WHERE id = %s", review_id) == [("verification_churn",)]

    await abuse.decide(review_id, "suspended", "oncall", "probing hosts it does not own")
    assert sql("SELECT suspended_at IS NOT NULL FROM organizations WHERE id = %s", org.org_id) == [(True,)]
    blocked = await _load(api, org, h, target, 10)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "ORG_SUSPENDED"
    await drain_in_process()
    assert mail.sent[-1] == (org.email, "Your organization has been suspended")


async def test_established_orgs_and_reviewed_hosts_are_not_held(api: httpx.AsyncClient, mail: Outbox) -> None:
    org = make_org("settled", plan="indie")
    sql("UPDATE organizations SET created_at = now() - interval '30 days' WHERE id = %s", org.org_id)
    h = await _ready(api, org)
    target = _target(org, "api.settled.example")
    run = await _load(api, org, h, target, 450)
    assert run.status_code == 202, run.text
    assert sql("SELECT count(*) FROM abuse_reviews WHERE org_id = %s", org.org_id) == [(0,)]
