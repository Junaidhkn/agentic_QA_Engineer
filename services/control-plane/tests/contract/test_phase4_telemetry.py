"""Phase 4: OTLP gateway tenancy and ingest tokens."""

from __future__ import annotations

import httpx
import pytest
import respx

from reliability.platform.crypto import sign_token
from reliability.platform.settings import get_settings
from tests.conftest import OrgFixture, auth

pytestmark = pytest.mark.integration


@pytest.fixture
async def gateway() -> object:
    from reliability import otlp_gateway

    async with otlp_gateway.lifespan(otlp_gateway.app):
        transport = httpx.ASGITransport(app=otlp_gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://otlp") as client:
            yield client


@respx.mock
async def test_gateway_sets_tenant_from_token_not_client(
    gateway: httpx.AsyncClient, org_a: OrgFixture, org_b: OrgFixture
) -> None:
    collector = respx.post(f"{get_settings().otel_collector_url}/v1/traces").respond(200, content=b"")
    token = sign_token({"org": str(org_a.org_id), "project": str(org_a.project_id), "run": None}, "otlp", 60)
    resp = await gateway.post(
        "/v1/traces",
        content=b"\x0a\x00",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
            "X-Scope-OrgID": str(org_b.org_id),
        },
    )
    assert resp.status_code == 200
    sent = collector.calls.last.request
    assert sent.headers["X-Scope-OrgID"] == str(org_a.org_id)
    assert (await gateway.post("/v1/traces", content=b"", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (
        await gateway.post("/v1/other", content=b"", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 404


@respx.mock
async def test_project_ingest_tokens(api: httpx.AsyncClient, gateway: httpx.AsyncClient, org_a: OrgFixture) -> None:
    collector = respx.post(f"{get_settings().otel_collector_url}/v1/metrics").respond(200, content=b"")
    created = await api.post(
        f"/v1/projects/{org_a.project_id}/ingest-tokens", json={"name": "staging"}, headers=auth(org_a.email)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["env_example"]["OTEL_EXPORTER_OTLP_HEADERS"].endswith(body["token"])
    ok = await gateway.post("/v1/metrics", content=b"x", headers={"Authorization": f"Bearer {body['token']}"})
    assert ok.status_code == 200 and collector.calls.last.request.headers["X-Scope-OrgID"] == str(org_a.org_id)
    await api.delete(f"/v1/projects/{org_a.project_id}/ingest-tokens/{body['id']}", headers=auth(org_a.email))
    denied = await gateway.post("/v1/metrics", content=b"x", headers={"Authorization": f"Bearer {body['token']}"})
    assert denied.status_code == 401
