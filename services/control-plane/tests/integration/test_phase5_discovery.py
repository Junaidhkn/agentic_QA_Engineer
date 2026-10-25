"""Phase 5: discovery through the API — source archive → discovery job (LangGraph with the
Postgres checkpointer) → candidate workflows → review → coverage; OpenAPI specs; LLM metering
and quota-driven degradation; cancellation."""

from __future__ import annotations

import io
import tarfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from reliability.llm import gateway as llm_gateway
from reliability.llm.gateway import Gateway
from reliability.llm.providers import FakeClient
from reliability.platform.outbox import drain_in_process
from reliability.platform.settings import get_settings
from tests.conftest import OrgFixture, auth, sql

pytestmark = pytest.mark.integration

SHOP = Path(__file__).resolve().parents[4] / "benchmarks" / "reference-apps" / "fastapi-shop"


def shop_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in SHOP.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                tf.add(p, arcname=f"shop-main/{p.relative_to(SHOP)}")
    return buf.getvalue()


@pytest.fixture
def fake_llm() -> Any:
    fake = FakeClient()
    llm_gateway.set_gateway(Gateway(fake, get_settings()))
    yield fake
    llm_gateway.set_gateway(None)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _discover(api: httpx.AsyncClient, org: OrgFixture, h: dict[str, str]) -> dict[str, Any]:
    up = await api.post(
        f"/v1/projects/{org.project_id}/source-archives",
        content=shop_archive(),
        headers={**h, "Content-Type": "application/octet-stream"},
    )
    assert up.status_code == 201, up.text
    started = await api.post(
        f"/v1/projects/{org.project_id}/discoveries",
        headers={**h, **_idem()},
        json={"archive_artifact_id": up.json()["archive_artifact_id"]},
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"
    await drain_in_process()
    result: dict[str, Any] = (await api.get(f"/v1/discoveries/{started.json()['id']}", headers=h)).json()
    return result


JOURNEY = {
    "ir_version": "1.0",
    "key": "shop.buy-and-pay",
    "title": "Sign up, buy and pay",
    "kind": "api",
    "criticality": "normal",
    "source": {"type": "static", "evidence": ["route:POST /api/orders", "file:app/orders.py:31"]},
    "data": {"vars": {"email": {"faker": "internet.email"}, "pw": {"value": "Pw-123456!"}}},
    "steps": [
        {
            "id": "signup",
            "request": {
                "method": "POST",
                "path": "/auth/signup",
                "json": {"email": "{{ email }}", "password": "{{ pw }}", "name": "T"},
            },
        },
        {
            "id": "login",
            "request": {
                "method": "POST",
                "path": "/auth/login",
                "json": {"email": "{{ email }}", "password": "{{ pw }}"},
            },
            "extract": {"token": "$.access_token"},
        },
        {
            "id": "order",
            "request": {
                "method": "POST",
                "path": "/api/orders",
                "headers": {"Authorization": "Bearer {{ token }}"},
                "json": {"product_id": 1, "quantity": 1},
            },
            "extract": {"order_id": "$.id"},
        },
        {
            "id": "pay",
            "request": {
                "method": "POST",
                "path": "/api/orders/{{ order_id }}/pay",
                "headers": {"Authorization": "Bearer {{ token }}"},
            },
        },
    ],
}


async def test_discovery_from_archive_with_model_and_review(
    api: httpx.AsyncClient, org_a: OrgFixture, fake_llm: FakeClient
) -> None:
    fake_llm.register("discovery.synthesize", lambda r: {"workflows": [JOURNEY]})
    fake_llm.register("discovery.criticality", lambda r: {"assessments": []})
    h = auth(org_a.email)
    run = await _discover(api, org_a, h)
    assert run["status"] == "awaiting_review", run
    stats = run["stats"]
    assert stats["frameworks"] == ["fastapi"] and stats["routes"]["api"] == 12
    assert stats["from_model"] == 1 and stats["llm"]["enabled"] and stats["llm"]["tokens"] > 0
    assert [p["step"] for p in run["progress"]][:3] == ["snapshot", "detect_stack", "extract_routes"]

    wfs = (await api.get(f"/v1/projects/{org_a.project_id}/workflows?status=candidate", headers=h)).json()
    keys = {w["key"]: w for w in wfs}
    assert {"auth.signup-and-login", "orders.create", "orders.lifecycle", "shop.buy-and-pay"} <= set(keys)
    assert keys["shop.buy-and-pay"]["criticality"] == "critical"  # rules floor: payment flow
    assert keys["orders.lifecycle"]["destructive"] is True
    assert all(w["source"] == "static" and 0 < float(w["discovery_confidence"]) <= 1 for w in wfs)

    # LLM calls are metered per org: llm_usage rows and the monthly token ledger.
    usage = sql("SELECT purpose, outcome FROM llm_usage WHERE org_id = %s ORDER BY purpose", org_a.org_id)
    assert ("discovery.synthesize", "ok") in usage and ("discovery.criticality", "ok") in usage
    tokens = sql("SELECT sum(quantity) FROM usage_ledger WHERE org_id = %s AND kind = 'llm_tokens'", org_a.org_id)
    assert tokens[0][0] > 0

    # Review: approve one candidate; a second discovery must not overwrite it.
    approved = keys["orders.create"]["id"]
    review = await api.post(
        f"/v1/projects/{org_a.project_id}/workflows:bulk-review",
        headers=h,
        json={"approve": [approved], "reject": [keys["orders.pay"]["id"]]},
    )
    assert review.status_code == 200, review.text
    again = await _discover(api, org_a, h)
    assert again["stats"]["persisted"]["created"] == 0 and again["stats"]["persisted"]["unchanged"] >= 2
    wf = (await api.get(f"/v1/workflows/{approved}", headers=h)).json()
    assert wf["status"] == "approved" and wf["spec_version"] == 1

    cov = (await api.get(f"/v1/projects/{org_a.project_id}/coverage", headers=h)).json()
    assert cov["totals"]["approved"] == 1 and cov["totals"]["tested"] == 0
    assert cov["totals"]["discovered"] == len(wfs) - 1  # rejected workflows are not counted


async def test_quota_exhaustion_degrades_to_baseline(
    api: httpx.AsyncClient, org_a: OrgFixture, fake_llm: FakeClient
) -> None:
    fake_llm.register("discovery.synthesize", lambda r: {"workflows": [JOURNEY]})
    sql(
        "INSERT INTO usage_ledger (id, org_id, period, kind, quantity, entry_type) "
        "VALUES (gen_random_uuid(), %s, date_trunc('month', now())::date, 'llm_tokens', 999999999, 'commit')",
        org_a.org_id,
    )
    run = await _discover(api, org_a, auth(org_a.email))
    assert run["status"] == "budget_exhausted" and run["stats"]["from_model"] == 0
    assert run["stats"]["persisted"]["created"] > 0 and not fake_llm.calls
    assert sql("SELECT outcome FROM llm_usage WHERE org_id = %s", org_a.org_id) == [("budget_denied",)]


async def test_org_opt_out_skips_model(api: httpx.AsyncClient, org_a: OrgFixture, fake_llm: FakeClient) -> None:
    sql("UPDATE organizations SET llm_code_context_enabled = false WHERE id = %s", org_a.org_id)
    run = await _discover(api, org_a, auth(org_a.email))
    assert run["status"] == "awaiting_review" and run["stats"]["llm"]["enabled"] is False
    assert not fake_llm.calls


OPENAPI = """{"openapi": "3.1.0", "info": {"title": "x", "version": "1"},
 "paths": {"/api/coupons": {"get": {"operationId": "list"}, "post": {"operationId": "create",
   "requestBody": {"content": {"application/json": {"example": {"code": "SAVE10", "uses": 1}}}}}},
  "/api/coupons/{id}": {"get": {"operationId": "get"}}}}"""


async def test_openapi_only_discovery_and_validation(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    h = auth(org_a.email)
    none = await api.post(f"/v1/projects/{org_a.project_id}/discoveries", headers={**h, **_idem()}, json={})
    assert none.status_code == 409
    bad = await api.post(
        f"/v1/projects/{org_a.project_id}/openapi-specs", headers=h, json={"document": '{"swagger": "2.0"}'}
    )
    assert bad.status_code == 422
    spec = await api.post(f"/v1/projects/{org_a.project_id}/openapi-specs", headers=h, json={"document": OPENAPI})
    assert spec.status_code == 201 and spec.json()["operation_count"] == 3
    started = await api.post(
        f"/v1/projects/{org_a.project_id}/discoveries",
        headers={**h, **_idem()},
        json={"openapi_spec_ids": [spec.json()["id"]]},
    )
    assert started.status_code == 202, started.text
    conflict = await api.post(
        f"/v1/projects/{org_a.project_id}/discoveries",
        headers={**h, **_idem()},
        json={"openapi_spec_ids": [spec.json()["id"]]},
    )
    assert conflict.status_code == 409  # one active discovery per project
    await drain_in_process()
    run = (await api.get(f"/v1/discoveries/{started.json()['id']}", headers=h)).json()
    assert run["status"] == "awaiting_review" and run["stats"]["operations"] == 3
    wfs = (await api.get(f"/v1/projects/{org_a.project_id}/workflows", headers=h)).json()
    create = next(w for w in wfs if w["key"] == "coupons.create")
    assert create["source"] == "openapi"
    assert create["spec"]["steps"][0]["request"]["json"] == {"code": "SAVE10", "uses": 1}


async def test_cancel_and_tenant_isolation(api: httpx.AsyncClient, org_a: OrgFixture, org_b: OrgFixture) -> None:
    h = auth(org_a.email)
    spec = await api.post(f"/v1/projects/{org_a.project_id}/openapi-specs", headers=h, json={"document": OPENAPI})
    started = (
        await api.post(
            f"/v1/projects/{org_a.project_id}/discoveries",
            headers={**h, **_idem()},
            json={"openapi_spec_ids": [spec.json()["id"]]},
        )
    ).json()
    assert (await api.get(f"/v1/discoveries/{started['id']}", headers=auth(org_b.email))).status_code == 404
    cancelled = await api.post(f"/v1/discoveries/{started['id']}:cancel", headers=h)
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    await drain_in_process()
    assert (await api.get(f"/v1/discoveries/{started['id']}", headers=h)).json()["status"] == "cancelled"
    assert (await api.get(f"/v1/projects/{org_a.project_id}/workflows", headers=h)).json() == []
    again = await api.post(f"/v1/discoveries/{started['id']}:cancel", headers=h)
    assert again.status_code == 409
