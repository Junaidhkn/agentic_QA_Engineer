"""Phase 11: legal documents, Terms/AUP acceptance (load-testing consent) before runs, and the
public plans list."""

from __future__ import annotations

import httpx
import pytest

from reliability.modules.legal import service as legal
from tests.conftest import add_member, auth, make_org, sql
from tests.integration.test_phase11_abuse import _load, _ready, _target

pytestmark = pytest.mark.integration
V = legal.DOCUMENTS["aup"].version


async def test_documents_are_public(api: httpx.AsyncClient) -> None:
    docs = (await api.get("/v1/legal/documents")).json()
    assert [d["document"] for d in docs] == ["terms", "aup", "dpa", "subprocessors"]
    assert {d["document"] for d in docs if d["required_for_runs"]} == {"terms", "aup"}
    aup = (await api.get("/v1/legal/documents/aup")).json()
    assert aup["version"] == V and "load-testing consent" in aup["markdown"]
    assert "Draft for legal review" in aup["markdown"]
    assert (await api.get("/v1/legal/documents/privacy-policy")).status_code == 404


async def test_runs_need_current_terms_and_aup(api: httpx.AsyncClient) -> None:
    org = make_org("newco", accept_terms=False)
    h = await _ready(api, org)
    target = _target(org, "app.newco.example")

    refused = await _load(api, org, h, target, 5)
    assert refused.status_code == 403, refused.text
    err = refused.json()["error"]
    assert err["code"] == "TERMS_NOT_ACCEPTED" and err["details"]["missing"] == ["terms", "aup"]
    status = (await api.get(f"/v1/orgs/{org.org_id}/legal", headers=h)).json()
    assert status["missing_for_runs"] == ["terms", "aup"] and status["accepted"] == []

    # Only owners/admins accept, only the current version, only acceptable documents.
    eng = "eng@newco.example"
    add_member(org, eng, "engineer")
    url = f"/v1/orgs/{org.org_id}/legal-acceptances"
    assert (await api.post(url, headers=auth(eng), json={"document": "aup", "version": V})).status_code == 403
    stale = await api.post(url, headers=h, json={"document": "aup", "version": "2020-01-01"})
    assert stale.status_code == 409 and V in stale.json()["error"]["message"]
    assert (await api.post(url, headers=h, json={"document": "subprocessors", "version": V})).status_code == 422

    after_aup = await api.post(
        url, headers={**h, "User-Agent": "pytest-browser"}, json={"document": "aup", "version": V}
    )
    assert after_aup.status_code == 200 and after_aup.json()["missing_for_runs"] == ["terms"]
    again = await api.post(url, headers=h, json={"document": "aup", "version": V})  # idempotent
    assert len(again.json()["accepted"]) == 1
    await api.post(url, headers=h, json={"document": "terms", "version": legal.DOCUMENTS["terms"].version})
    await api.post(url, headers=h, json={"document": "dpa", "version": legal.DOCUMENTS["dpa"].version})

    allowed = await _load(api, org, h, target, 5)
    assert allowed.status_code == 202, allowed.text
    (ua, user_id) = sql(
        "SELECT user_agent, user_id FROM legal_acceptances WHERE org_id = %s AND document = 'aup'", org.org_id
    )[0]
    assert ua == "pytest-browser" and user_id == org.user_id
    assert sql("SELECT count(*) FROM audit_events WHERE org_id = %s AND action = 'legal.accepted'", org.org_id) == [
        (3,)
    ]


async def test_acceptance_is_per_org(api: httpx.AsyncClient) -> None:
    a, b = make_org("acc-a"), make_org("acc-b", accept_terms=False)
    other = await api.get(f"/v1/orgs/{b.org_id}/legal", headers=auth(a.email))
    assert other.status_code in (403, 404)
    assert (await api.get(f"/v1/orgs/{a.org_id}/legal", headers=auth(a.email))).json()["missing_for_runs"] == []
    assert sql("SELECT count(*) FROM legal_acceptances WHERE org_id = %s", b.org_id) == [(0,)]


async def test_plans_are_public_and_ordered(api: httpx.AsyncClient) -> None:
    plans = (await api.get("/v1/plans")).json()
    assert [(p["id"], p["name"]) for p in plans] == [
        ("free", "Free"),
        ("indie", "Indie"),
        ("startup", "Startup"),
        ("team", "Team"),
    ]
    free, team = plans[0], plans[-1]
    assert free["price_usd_month"] == 0 and free["features"] == {"repair": False, "browser_exploration": False}
    assert free["max_vus"] == 50 and free["allowed_profile_types"] == ["smoke", "baseline"]
    assert team["price_usd_month"] is None  # contact sales
