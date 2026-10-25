"""Phase 8 end to end (UF-6): seeded missing-index defect → finding → diagnosis → repair agent
(an oracle stand-in for the model, driving the real graph and tools) → regression test confirmed
failing on base in a sandbox → fix confirmed by local checks → independent verification (build,
fail-to-pass 3×/3×, functional suite, security, interleaved load comparison with BCa CI, diff
policy) → approval → deterministic pull request via a mocked GitHub (exactly once, never merged).
Skipped without k6."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reliability.llm import Gateway, set_gateway
from reliability.llm.fakes import default_fake_client
from reliability.modules.integrations.github import client as gh_client
from reliability.platform.outbox import drain_in_process
from reliability.platform.settings import get_settings
from tests.conftest import auth, sql
from tests.integration.test_phase3_e2e import K6, live_api  # noqa: F401
from tests.support.defects import CASES, run_case, runner
from tests.support.repairs import RUNNER_TYPES, check_outputs, fixture, install_oracle, wait_repair

pytestmark = pytest.mark.integration

FAST = {
    "RE_VERIFICATION_LOAD_HOLD_S": "15",
    "RE_VERIFICATION_LOAD_SETTLE_S": "5",
    "RE_VERIFICATION_PROBE_RUNS": "2",
    "RE_VERIFICATION_BOOTSTRAP_ITERATIONS": "2000",
}


@pytest.fixture
def fast_verification(monkeypatch: pytest.MonkeyPatch) -> Any:
    for k, v in FAST.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def oracle_llm() -> Any:
    fake = default_fake_client()
    set_gateway(Gateway(fake, get_settings()))
    yield fake
    set_gateway(None)


async def test_missing_index_is_repaired_verified_approved_and_proposed(
    live_api: str,  # noqa: F811
    tmp_path: Path,
    fast_verification: None,
    oracle_llm: Any,
) -> None:
    case = next(c for c in CASES if c.defect == "missing_index")
    fx = fixture("fastapi-tasks", "missing_index")
    async with httpx.AsyncClient(base_url=live_api, timeout=60) as client:
        out = await run_case(client, live_api, tmp_path, case, K6)
        assert out.diagnosis and out.rank_hit == 1, out.to_dict()
        top = out.diagnosis["hypotheses"][0]
        email = sql(
            "SELECT u.email FROM users u JOIN memberships m ON m.user_id = u.id JOIN findings f "
            "ON f.org_id = m.org_id WHERE f.id = %s",
            out.finding["id"],  # type: ignore[index]
        )[0][0]
        h = auth(email)
        install_oracle(oracle_llm, fx)

        started = await client.post(
            f"/v1/hypotheses/{top['id']}/repairs", headers={**h, "Idempotency-Key": "repair-1"}, json={}
        )
        assert started.status_code == 202, started.text
        repair = started.json()
        cfg = runner(live_api, tmp_path, RUNNER_TYPES, K6)
        done = await wait_repair(client, h, repair["id"], cfg)
        if done["status"] != "awaiting_approval":
            print("CHECKS", json.dumps(await check_outputs(), indent=1)[:20000])
        assert done["status"] == "awaiting_approval", (
            done["status"],
            done["status_reason"],
            done.get("verification"),
            await check_outputs(),
        )

        # The agent's candidate: the fix plus a regression test, inside policy.
        assert sorted(done["regression_test_paths"]) == [fx.test_path]
        assert "+++ b/alembic/versions/0003_assignee_index_always.py" in done["diff"]
        assert done["agent_summary_label"] == "AI-generated explanation"
        gates = {g["gate"]: g for g in done["verification"]["gates"]}
        assert {g for g, v in gates.items() if v["applicable"]} == {
            "build",
            "fail_to_pass",
            "functional_suite",
            "security",
            "load_comparison",
            "diff_policy",
        }
        assert all(g["status"] == "passed" for g in gates.values() if g["applicable"]), gates
        f2p = gates["fail_to_pass"]["result"]
        assert len(f2p["base_exit_codes"]) == 3 and all(c != 0 for c in f2p["base_exit_codes"])
        assert f2p["patched_exit_codes"] == [0, 0, 0]
        assert len(gates["load_comparison"]["run_ids"]) == 6
        main = next(c for c in done["verification"]["comparisons"] if c["scope"] == "endpoint:GET /api/tasks")
        assert main["verdict"] == "improved" and main["ci_high_pct"] < -main["noise_floor_pct"], main
        assert main["method"] == "bootstrap_bca_10k" and len(main["baseline_run_ids"]) == 3

        # Verification runs never become findings, and the agent's own checks were sandbox jobs.
        assert sql("SELECT count(*) FROM runs WHERE config_snapshot ? 'verification'")[0][0] == 7
        assert (
            sql(
                "SELECT count(*) FROM findings f JOIN runs r ON r.id = f.last_run_id "
                "WHERE r.config_snapshot ? 'verification'"
            )[0][0]
            == 0
        )
        assert sql("SELECT count(*) FROM execution_jobs WHERE job_type = 'repair_check'")[0][0] == 4
        finding_status = sql("SELECT status FROM findings WHERE id = %s", out.finding["id"])[0][0]  # type: ignore[index]
        assert finding_status == "fix_proposed"

        patch = await client.get(f"/v1/repairs/{repair['id']}/patch", headers=h)
        assert patch.status_code == 200 and patch.headers["content-type"].startswith("text/x-diff")

        # Approve → no GitHub repository (the source was an uploaded archive): patch download only.
        approval = done["approval"]
        decided = await client.post(
            f"/v1/approvals/{approval['id']}:decide", headers=h, json={"decision": "approved", "reason": "lgtm"}
        )
        assert decided.status_code == 200, decided.text
        await drain_in_process()
        after = (await client.get(f"/v1/repairs/{repair['id']}", headers=h)).json()
        assert after["status"] == "approved" and "no GitHub repository" in after["status_reason"]

        await _pull_request_is_created_once(client, h, repair["id"], out.finding["id"])  # type: ignore[index]


async def _pull_request_is_created_once(
    client: httpx.AsyncClient, h: dict[str, str], repair_id: str, finding_id: str
) -> None:
    """With a connected repository and a real base commit, the approved repair becomes exactly one
    PR on a reliability/ branch through create-only GitHub calls."""
    org_id, project_id = sql("SELECT org_id, project_id FROM findings WHERE id = %s", finding_id)[0]
    sql(
        "INSERT INTO github_installations (id, org_id, installation_id, account_login) "
        "VALUES (gen_random_uuid(), %s, 4242, 'acme')",
        org_id,
    )
    sql(
        "INSERT INTO repositories (id, org_id, project_id, installation_id, github_repo_id, full_name, default_branch) "
        "SELECT gen_random_uuid(), %s, %s, id, 99, 'acme/tasks', 'main' FROM github_installations "
        "WHERE installation_id = 4242",
        org_id,
        project_id,
    )
    sql("UPDATE repair_attempts SET base_commit_sha = %s WHERE id = %s", "a" * 40, repair_id)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    gh_client.set_client_override(gh_client.GitHubClient("12345", pem, "https://api.github.com"))
    api = "https://api.github.com"
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{api}/app/installations/4242/access_tokens").respond(
                201, json={"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"}
            )
            mock.get(f"{api}/repos/acme/tasks/git/commits/{'a' * 40}").respond(200, json={"tree": {"sha": "t0"}})
            blobs = mock.post(f"{api}/repos/acme/tasks/git/blobs").respond(201, json={"sha": "b1"})
            mock.post(f"{api}/repos/acme/tasks/git/trees").respond(201, json={"sha": "t1"})
            mock.post(f"{api}/repos/acme/tasks/git/commits").respond(201, json={"sha": "c1"})
            refs = mock.post(f"{api}/repos/acme/tasks/git/refs").respond(201, json={})
            pulls = mock.post(f"{api}/repos/acme/tasks/pulls").respond(
                201, json={"number": 7, "html_url": "https://github.com/acme/tasks/pull/7"}
            )
            from reliability.modules.repairs import jobs as repair_jobs

            for _ in range(2):  # a retried job must not open a second PR
                await repair_jobs.open_pr({"attempt_id": repair_id, "org_id": str(org_id)})
            assert pulls.call_count == 1 and refs.call_count == 1 and blobs.call_count == 2
            branch = json.loads(refs.calls[0].request.content)["ref"]
            assert re.fullmatch(r"refs/heads/reliability/[0-9a-f]{8}-[0-9a-f]{8}", branch)
            pr = json.loads(pulls.calls[0].request.content)
            assert pr["title"].startswith("fix(reliability): ") and pr["base"] == "main"
            for part in (
                "## Verification gates",
                "fail to pass",
                "## Before / after",
                "BCa",
                "## Explanation (AI-generated)",
                "review it like any other contribution",
            ):
                assert part in pr["body"], part
            # Create-only: no merge, no ref updates, no pushes to existing branches.
            methods = {(c.request.method, c.request.url.path) for c in mock.calls}
            assert not any("merge" in path or method in ("PUT", "PATCH", "DELETE") for method, path in methods)
    finally:
        gh_client.set_client_override(None)
    detail = (await client.get(f"/v1/repairs/{repair_id}", headers=h)).json()
    assert detail["status"] == "pr_opened"
    assert detail["pull_request"]["url"] == "https://github.com/acme/tasks/pull/7"
    assert sql("SELECT count(*) FROM pull_requests WHERE repair_attempt_id = %s", repair_id)[0][0] == 1


def test_github_client_has_no_merge_path() -> None:
    """ADR-0017: the platform can open pull requests but has no code path that merges them."""
    names = {n for n in dir(gh_client.GitHubClient) if not n.startswith("_")}
    assert not {n for n in names if "merge" in n or "push" in n or "delete" in n}
    source = Path(gh_client.__file__).read_text()
    assert "/merge" not in source and ".put(" not in source and ".patch(" not in source and ".delete(" not in source
