"""Phase 2 contract tests: identity, tenancy, projects, targets, secrets, GitHub."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reliability.modules.integrations.github import client as gh_client
from reliability.modules.notifications.service import LogEmailSender
from reliability.modules.targets import safety
from reliability.modules.targets import service as targets_service
from reliability.platform.settings import get_settings
from tests.conftest import OrgFixture, add_member, auth, sql

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(host: str) -> list[str]:
        return {"internal.example.com": ["10.0.0.5"], "meta.example.com": ["169.254.169.254"]}.get(
            host, ["93.184.216.34"]
        )

    monkeypatch.setattr(safety, "system_resolver", resolver)


# ------------------------------------------------------------ identity ----
async def test_new_user_gets_an_org_as_owner(api: httpx.AsyncClient) -> None:
    resp = await api.get("/v1/me", headers=auth("newbie@example.com"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "newbie@example.com" and [m["role"] for m in body["memberships"]] == ["owner"]
    again = await api.get("/v1/me", headers=auth("newbie@example.com"))
    assert again.json()["memberships"] == body["memberships"]


async def test_missing_or_bad_token_is_401(api: httpx.AsyncClient) -> None:
    assert (await api.get("/v1/me")).json()["error"]["code"] == "UNAUTHENTICATED"
    assert (await api.get("/v1/me", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_dev_auth_is_rejected_outside_local(api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "env", "staging", raising=False)
    try:
        assert (await api.get("/v1/me", headers=auth("x@example.com"))).status_code == 401
    finally:
        monkeypatch.setattr(get_settings(), "env", "test", raising=False)


# ------------------------------------------------------- tenancy, RBAC ----
async def test_cross_tenant_resources_return_404(api: httpx.AsyncClient, org_a: OrgFixture, org_b: OrgFixture) -> None:
    resp = await api.get(f"/v1/projects/{org_b.project_id}", headers=auth(org_a.email))
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"
    for path in (
        f"/v1/projects/{org_b.project_id}/targets",
        f"/v1/projects/{org_b.project_id}/secrets",
        f"/v1/orgs/{org_b.org_id}/members",
        f"/v1/orgs/{org_b.org_id}/audit-events",
    ):
        assert (await api.get(path, headers=auth(org_a.email))).status_code == 404, path
    listed = await api.get("/v1/projects", headers=auth(org_a.email))
    assert [p["id"] for p in listed.json()] == [str(org_a.project_id)]


async def test_role_matrix_for_targets(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    add_member(org_a, "viewer@example.com", "viewer")
    add_member(org_a, "eng@example.com", "engineer")
    body = {"base_url": "https://staging.example.com", "environment": "staging"}
    for email, status in (("viewer@example.com", 403), ("eng@example.com", 403), (org_a.email, 201)):
        resp = await api.post(f"/v1/projects/{org_a.project_id}/targets", json=body, headers=auth(email))
        assert resp.status_code == status, (email, resp.text)
    listed = await api.get(f"/v1/projects/{org_a.project_id}/targets", headers=auth("viewer@example.com"))
    assert listed.status_code == 200 and listed.json()[0]["host"] == "staging.example.com"


async def test_unknown_fields_rejected(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    resp = await api.post("/v1/projects", json={"name": "x", "evil": 1}, headers=auth(org_a.email))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_invite_and_accept(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    LogEmailSender.sent.clear()
    resp = await api.post(
        f"/v1/orgs/{org_a.org_id}/invitations",
        json={"email": "new@example.com", "role": "engineer"},
        headers=auth(org_a.email),
    )
    assert resp.status_code == 201
    token = LogEmailSender.sent[-1][2].rsplit("/", 1)[1]
    wrong = await api.post(f"/v1/invitations/{token}:accept", headers=auth("other@example.com"))
    assert wrong.status_code == 403
    ok = await api.post(f"/v1/invitations/{token}:accept", headers=auth("new@example.com"))
    assert ok.status_code == 200 and ok.json()["role"] == "engineer"
    again = await api.post(f"/v1/invitations/{token}:accept", headers=auth("new@example.com"))
    assert again.status_code == 404


async def test_last_owner_cannot_be_demoted(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    resp = await api.patch(
        f"/v1/orgs/{org_a.org_id}/members/{org_a.user_id}", json={"role": "admin"}, headers=auth(org_a.email)
    )
    assert resp.status_code == 409


async def test_plan_project_limit(api: httpx.AsyncClient) -> None:
    from tests.conftest import make_org

    org = make_org("freeorg", plan="free")  # free plan: 1 project, already has one
    resp = await api.post("/v1/projects", json={"name": "second"}, headers=auth(org.email))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"


# -------------------------------------------------------------- API keys ----
async def test_api_key_scopes_and_revocation(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    created = await api.post(
        f"/v1/orgs/{org_a.org_id}/api-keys",
        json={"name": "ci", "scopes": ["projects:read", "runs:write"]},
        headers=auth(org_a.email),
    )
    assert created.status_code == 201
    secret = created.json()["secret"]
    key_headers = {"Authorization": f"Bearer {secret}"}
    assert (await api.get(f"/v1/projects/{org_a.project_id}", headers=key_headers)).status_code == 200
    forbidden = await api.post(
        f"/v1/projects/{org_a.project_id}/targets",
        headers=key_headers,
        json={"base_url": "https://a.example.com", "environment": "staging"},
    )
    assert forbidden.status_code == 403
    listed = await api.get(f"/v1/orgs/{org_a.org_id}/api-keys", headers=auth(org_a.email))
    assert "secret" not in listed.json()[0]
    await api.delete(f"/v1/orgs/{org_a.org_id}/api-keys/{created.json()['id']}", headers=auth(org_a.email))
    assert (await api.get(f"/v1/projects/{org_a.project_id}", headers=key_headers)).status_code == 401
    tampered = secret[:-2] + ("AA" if not secret.endswith("AA") else "BB")
    assert (await api.get("/v1/me", headers={"Authorization": f"Bearer {tampered}"})).status_code == 401


# --------------------------------------------------------------- targets ----
async def test_target_ssrf_protection(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    for url in (
        "https://internal.example.com",
        "http://meta.example.com",
        "http://localhost:8080",
        "ftp://example.com",
        "https://user:pw@example.com",
    ):
        resp = await api.post(
            f"/v1/projects/{org_a.project_id}/targets",
            headers=auth(org_a.email),
            json={"base_url": url, "environment": "staging"},
        )
        assert resp.status_code == 422, url
        assert resp.json()["error"]["code"] in ("UNSAFE_TARGET", "VALIDATION_FAILED")


async def test_target_verification_lifecycle(
    api: httpx.AsyncClient, org_a: OrgFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await api.post(
        f"/v1/projects/{org_a.project_id}/targets",
        headers=auth(org_a.email),
        json={"base_url": "https://Staging.Example.com/app/", "environment": "staging"},
    )
    target = created.json()
    assert target["status"] == "pending" and target["base_url"] == "https://staging.example.com/app"
    expected = target["verification"]["dns_txt"]["value"]

    async def no_http(_: object) -> None:
        return None

    monkeypatch.setattr(targets_service, "http_well_known", no_http)
    monkeypatch.setattr(targets_service, "dns_txt_records", lambda name: _async([]))
    failed = await api.post(f"/v1/targets/{target['id']}:verify", headers=auth(org_a.email))
    assert failed.json()["status"] == "failed"

    monkeypatch.setattr(targets_service, "dns_txt_records", lambda name: _async(["v=spf1 -all", expected]))
    ok = await api.post(f"/v1/targets/{target['id']}:verify", headers=auth(org_a.email))
    assert ok.json()["status"] == "verified" and ok.json()["verification_method"] == "dns_txt"

    monkeypatch.setattr(targets_service, "dns_txt_records", lambda name: _async([]))
    lost = await api.post(f"/v1/targets/{target['id']}:verify", headers=auth(org_a.email))
    assert lost.json()["status"] == "failed"
    assert sql("SELECT topic FROM outbox")[0][0] == "targets.revoked"
    actions = [r[0] for r in sql("SELECT action FROM audit_events ORDER BY seq")]
    assert "target.verified" in actions and actions[-1] == "target.verification_failed"


async def _async(value: object) -> object:
    return value


@respx.mock
async def test_http_verification_does_not_follow_cross_host_redirects() -> None:
    from reliability.modules.targets.models import Target

    t = Target(host="app.example.com", base_url="https://app.example.com", verification_token="tok")
    respx.get("https://app.example.com/.well-known/reliability-engineer.txt").respond(
        302, headers={"location": "https://attacker.example.net/.well-known/reliability-engineer.txt"}
    )
    assert await targets_service.http_well_known(t) is None
    respx.get("https://app.example.com/.well-known/reliability-engineer.txt").respond(200, text="re-verify=tok")
    assert await targets_service.http_well_known(t) == "re-verify=tok"


# --------------------------------------------------------------- secrets ----
async def test_secrets_are_write_only_and_decryptable_for_runs(api: httpx.AsyncClient, org_a: OrgFixture) -> None:
    resp = await api.put(
        f"/v1/projects/{org_a.project_id}/secrets/TEST_PASSWORD", json={"value": "hunter2"}, headers=auth(org_a.email)
    )
    assert resp.status_code == 204
    listed = await api.get(f"/v1/projects/{org_a.project_id}/secrets", headers=auth(org_a.email))
    assert listed.json()[0]["name"] == "TEST_PASSWORD" and "value" not in listed.json()[0]
    raw = sql("SELECT ciphertext FROM project_secrets")[0][0]
    assert b"hunter2" not in bytes(raw)
    bad = await api.put(
        f"/v1/projects/{org_a.project_id}/secrets/lower", json={"value": "x"}, headers=auth(org_a.email)
    )
    assert bad.status_code == 422

    from reliability.modules.secrets import service as secrets
    from reliability.platform.actor import SYSTEM
    from reliability.platform.db import tenant_tx

    async with tenant_tx(org_ids=[org_a.org_id]) as s:
        values = await secrets.resolve_for_run(s, SYSTEM, org_a.org_id, org_a.project_id)
    assert values == {"TEST_PASSWORD": "hunter2"}


# ---------------------------------------------------------------- GitHub ----
@pytest.fixture
def github_app(monkeypatch: pytest.MonkeyPatch) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    gh_client.set_client_override(gh_client.GitHubClient("12345", pem, "https://api.github.com"))
    s = get_settings()
    monkeypatch.setattr(s, "github_app_slug", "reliability-test", raising=False)
    from pydantic import SecretStr

    monkeypatch.setattr(s, "github_webhook_secret", SecretStr("whsec"), raising=False)
    yield "whsec"
    gh_client.set_client_override(None)


@respx.mock
async def test_github_install_link_repo_and_webhooks(
    api: httpx.AsyncClient, org_a: OrgFixture, github_app: str
) -> None:
    url = (await api.post("/v1/integrations/github/install-url", headers=auth(org_a.email))).json()["url"]
    state = url.split("state=")[1]
    respx.get("https://api.github.com/app/installations/777").respond(
        200, json={"id": 777, "account": {"login": "acme"}}
    )
    respx.post("https://api.github.com/app/installations/777/access_tokens").respond(
        201, json={"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"}
    )
    respx.get("https://api.github.com/installation/repositories").respond(
        200, json={"repositories": [{"id": 42, "full_name": "acme/shop", "default_branch": "main", "private": True}]}
    )

    cb = await api.get("/v1/integrations/github/callback", params={"installation_id": 777, "state": state})
    assert cb.status_code == 302 and "github=installed" in cb.headers["location"]
    forged = await api.get("/v1/integrations/github/callback", params={"installation_id": 777, "state": "forged"})
    assert "github=error" in forged.headers["location"]

    repos = (await api.get("/v1/integrations/github/repositories", headers=auth(org_a.email))).json()
    assert repos[0]["full_name"] == "acme/shop"
    linked = await api.put(
        f"/v1/projects/{org_a.project_id}/repository",
        headers=auth(org_a.email),
        json={"installation_id": repos[0]["installation_id"], "github_repo_id": 42},
    )
    assert linked.status_code == 200 and linked.json()["full_name"] == "acme/shop"

    payload = json.dumps({"action": "deleted", "installation": {"id": 777}}).encode()
    sig = "sha256=" + hmac.new(b"whsec", payload, hashlib.sha256).hexdigest()
    bad = await api.post(
        "/webhooks/github",
        content=payload,
        headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert bad.status_code == 401
    ok = await api.post(
        "/webhooks/github",
        content=payload,
        headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": sig},
    )
    assert ok.json()["result"] == "uninstalled"
    dup = await api.post(
        "/webhooks/github",
        content=payload,
        headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": sig},
    )
    assert dup.json()["result"] == "duplicate"
    assert sql("SELECT count(*) FROM repositories")[0][0] == 0  # cascade with installation


def test_branch_prefix_enforced() -> None:
    client = gh_client.GitHubClient("1", "k")
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(client.open_pull_request("t", "a/b", "main", "main", "x", "y"))
