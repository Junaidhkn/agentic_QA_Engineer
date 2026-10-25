"""Sandbox environments (ADR-0009, ADR-0011): compose hardening, generated projects, Dockerfile
templates, Docker command construction, stub server, source extraction and the process
sandbox end to end (Postgres when ``RA_TEST_PG_URL`` / a local server is available)."""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from runner_agent.config import Config
from runner_agent.sandbox_env import compose
from runner_agent.sandbox_env.base import SandboxError, extract_source
from runner_agent.sandbox_env.docker import buildx_build_args, buildx_create_args, compose_args
from runner_agent.sandbox_env.process import ProcessSandbox, database_url
from runner_agent.sandbox_env.stub import StubServer

IMAGES = {"postgres": "postgres:16.6", "redis": "redis:7.4.1", "stub": "runner:1", "collector": "otelcol:1"}
PLAN = {
    "strategy": "template",
    "template": "python",
    "language": "python",
    "port": 8000,
    "health_path": "/health",
    "install": [["pip", "install", "--no-cache-dir", "fastapi"]],
    "build": [],
    "start": ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "{port}"],
    "env": {"NODE_ENV": "production"},
    "services": {"postgres": {"image": "postgres:16.6", "user": "sandbox", "database": "app"}},
    "database_env": ["DATABASE_URL"],
    "database_scheme": "postgresql+psycopg",
    "stubs": [{"name": "stripe", "env": "STRIPE_API_BASE", "routes": []}],
    "placeholders": ["SESSION_SECRET"],
}


def test_rewrite_compose_hardens_services(tmp_path: Path) -> None:
    doc = {
        "services": {
            "web": {
                "build": ".",
                "ports": ["8080:3000"],
                "privileged": True,
                "cap_add": ["SYS_ADMIN"],
                "network_mode": "host",
                "environment": ["EXISTING=1"],
                "volumes": ["./data:/data"],
            },
            "db": {"image": "postgres:16", "environment": {"POSTGRES_PASSWORD": "x"}},
        }
    }
    plan = {
        **PLAN,
        "strategy": "compose",
        "app_service": "web",
        "port": 3000,
        "services": {"postgres": {"from_compose": "db", "user": "postgres", "database": "postgres"}},
    }
    out = compose.rewrite_compose(doc, plan, "abcdef123456789", "runsc", IMAGES, tmp_path, "run-1")
    web = out["services"]["web"]
    for forbidden in ("privileged", "cap_add", "network_mode", "ports"):
        assert forbidden not in web
    assert web["expose"] == ["3000"] and web["runtime"] == "runsc" and web["networks"] == ["run"]
    assert web["environment"]["EXISTING"] == "1"
    assert web["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://postgres:${RE_SANDBOX_PG_PASSWORD}@db:")
    assert web["environment"]["STRIPE_API_BASE"] == "http://stub:8089/stripe"
    assert web["environment"]["SESSION_SECRET"] == "sandbox-placeholder"
    assert "postgres" not in out["services"]  # the compose file's own database is used
    assert out["networks"]["run"] == {"external": True, "name": "re-run-abcdef123456"}
    assert all(s["runtime"] == "runsc" for s in out["services"].values())
    assert {"stub", "otel-collector"} <= set(out["services"])


@pytest.mark.parametrize("bad", [["/var/run/docker.sock:/var/run/docker.sock"], ["../../etc:/etc"], ["~/x:/x"]])
def test_rewrite_compose_rejects_host_mounts(tmp_path: Path, bad: list[str]) -> None:
    doc = {"services": {"web": {"build": ".", "volumes": bad}}}
    with pytest.raises(compose.ComposeError):
        compose.rewrite_compose(doc, {**PLAN, "app_service": "web"}, "j" * 12, "runsc", IMAGES, tmp_path)


def test_rewrite_compose_rejects_outside_build_context(tmp_path: Path) -> None:
    doc = {"services": {"web": {"build": {"context": "../other"}}}}
    with pytest.raises(compose.ComposeError):
        compose.rewrite_compose(doc, {**PLAN, "app_service": "web"}, "j" * 12, "runsc", IMAGES, tmp_path)


def test_generate_compose_for_template() -> None:
    out = compose.generate_compose(PLAN, "abcdef123456", "runsc", IMAGES, "re-sbx/abcdef123456:app", "run-1")
    app, pg = out["services"]["app"], out["services"]["postgres"]
    assert (
        app["cap_drop"] == ["ALL"]
        and app["expose"] == ["8000"]
        and app["depends_on"]["postgres"] == {"condition": "service_healthy"}
    )
    assert app["environment"]["DATABASE_URL"] == (
        "postgresql+psycopg://sandbox:${RE_SANDBOX_PG_PASSWORD}@postgres:5432/app"
    )
    assert app["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4318"
    assert "re.run_id=run-1" in app["environment"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert "pg_stat_statements" in " ".join(pg["command"]) and pg["cap_add"]
    assert "${RE_SANDBOX_PG_PASSWORD}" in pg["environment"]["POSTGRES_PASSWORD"]
    yaml.safe_dump(out)  # serializable


def test_render_dockerfile_uses_exec_form() -> None:
    text = compose.render_dockerfile(PLAN)
    assert 'RUN ["pip", "install", "--no-cache-dir", "fastapi"]' in text
    assert 'CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]' in text
    assert "USER 10001" in text and "auto_instrumentation" in text
    node = compose.render_dockerfile(
        {**PLAN, "template": "node", "install": [["npm", "ci"]], "start": ["npm", "run", "start"]}
    )
    assert 'RUN ["npm", "ci"]' in node and "auto-instrumentations-node" in node


def test_docker_build_commands_use_isolated_builder(tmp_path: Path) -> None:
    cfg = Config(driver="docker", build_cache_dir=str(tmp_path / "cache"), pip_index_url="https://mirror/pypi/simple")
    create = buildx_create_args(cfg, "abcdef1234567")
    assert "network=re-build-abcdef123456" in create
    assert any(a.startswith("env.https_proxy=http://re-build-egress-abcdef123456") for a in create)
    build = buildx_build_args(cfg, "abcdef1234567", tmp_path, tmp_path / "Dockerfile", "proj")
    assert build[build.index("--builder") + 1] == "re-build-abcdef123456"
    assert f"type=local,src={tmp_path / 'cache' / 'proj'}" in build
    assert "PIP_INDEX_URL=https://mirror/pypi/simple" in build
    args = compose_args("abcdef1234567", tmp_path / "src" / ".re-sandbox" / "compose.yaml", "up", "-d")
    assert args[:4] == ["compose", "-p", "re-sbx-abcdef123456", "--project-directory"]
    assert args[4] == str(tmp_path / "src")


def test_collector_config_forwards_with_run_token() -> None:
    cfg = compose.collector_config("https://otlp.example", "tok", None)
    assert '"https://otlp.example"' in cfg and '"Bearer tok"' in cfg


def test_stub_server_records_calls() -> None:
    stub = StubServer(
        [
            {
                "name": "stripe",
                "env": "X",
                "routes": [{"method": "POST", "path": "/v1/charges", "status": 201, "body": {"id": "ch_1"}}],
            }
        ]
    ).start()
    try:
        base = f"http://127.0.0.1:{stub.port}"
        assert httpx.post(f"{base}/stripe/v1/charges", json={"a": 1}).json() == {"id": "ch_1"}
        assert httpx.get(f"{base}/stripe/v1/other").status_code == 200
    finally:
        stub.stop()
    assert stub.summary()[0] == {"call": "stripe POST /v1/charges", "count": 1}


def test_extract_source_strips_root_and_blocks_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in {"repo-abc/app.py": b"x", "repo-abc/../../evil.py": b"bad"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    root = extract_source(buf.getvalue(), tmp_path / "a")
    assert (root / "app.py").read_bytes() == b"x" and not (tmp_path / "evil.py").exists()
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("main.py", "y")
        zf.writestr("../escape.py", "bad")
    root = extract_source(zbuf.getvalue(), tmp_path / "b")
    assert (root / "main.py").read_text() == "y" and not (tmp_path / "escape.py").exists()


def test_database_url() -> None:
    assert database_url("postgresql://u:p@h:5432/postgres", "sbx_1", "postgresql+psycopg") == (
        "postgresql+psycopg://u:p@h:5432/sbx_1"
    )


APP = """
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/health":
            body = {"ok": True}
        elif self.path == "/rows":
            import psycopg
            with psycopg.connect(os.environ["DATABASE_URL"]) as c:
                body = {"rows": c.execute("select count(*) from items").fetchone()[0]}
        elif self.path == "/charge":
            body = json.load(urllib.request.urlopen(os.environ["STRIPE_API_BASE"] + "/v1/charges"))
        else:
            body = {"secret": os.environ.get("SESSION_SECRET"), "api": os.environ.get("API_TOKEN")}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
"""
MIGRATE = """
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    c.execute("create table items (id serial primary key, name text)")
"""
SEED = """
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    c.execute("insert into items (name) select 'x' || g from generate_series(1, 25) g")
"""
PG_URL = (
    os.environ.get("RA_TEST_PG_URL")
    or os.environ.get("RE_TEST_PG_SUPERUSER_URL")
    or ("postgresql://postgres:postgres@localhost:5432/postgres")
)


def _pg_available() -> bool:
    try:
        import psycopg

        psycopg.connect(PG_URL, connect_timeout=2).close()
        return True
    except Exception:
        return False


async def test_process_sandbox_with_postgres_stub_and_secrets(tmp_path: Path) -> None:
    if not _pg_available():
        pytest.skip("no PostgreSQL available")
    src = tmp_path / "src"
    src.mkdir()
    (src / "server.py").write_text(APP)
    (src / "migrate.py").write_text(MIGRATE)
    (src / "seed.py").write_text(SEED)
    plan = {
        "strategy": "template",
        "template": "python",
        "language": "python",
        "root": "",
        "port": 8000,
        "health_path": "/health",
        "install": [],
        "build": [],
        "start": ["python", "server.py"],
        "env": {},
        "services": {"postgres": {"image": "postgres:16.6"}},
        "database_env": ["DATABASE_URL"],
        "database_scheme": "postgresql",
        "migrate": [["python", "migrate.py"]],
        "seed": [["python", "seed.py"]],
        "stubs": [
            {
                "name": "stripe",
                "env": "STRIPE_API_BASE",
                "routes": [{"method": "GET", "path": "/v1/charges", "body": {"data": []}}],
            }
        ],
        "placeholders": ["SESSION_SECRET"],
    }
    cfg = Config(env="test", sandbox_postgres_url=PG_URL, sandbox_inherit_site_packages=True)
    sbx = ProcessSandbox(cfg, "job-123456789", src, plan, {"API_TOKEN": "t0k"}, None, tmp_path / "sandbox.log")
    await sbx.build()
    base = await sbx.start()
    try:
        async with httpx.AsyncClient(base_url=base) as client:
            assert (await client.get("/rows")).json() == {"rows": 25}
            assert (await client.get("/charge")).json() == {"data": []}
            assert (await client.get("/env")).json() == {"secret": "sandbox-placeholder", "api": "t0k"}
        await sbx.sample()
        await sbx.collect()
    finally:
        await sbx.stop()
    r = sbx.report
    assert r.healthy and r.migrations == "applied" and r.seed == "applied" and "postgres" in r.services
    items = next(t for t in r.db["tables"] if t["relname"] == "items")
    assert items["n_live_tup"] == 25 and r.db["connections"]["max"] > 0
    assert r.stub_calls == [{"call": "stripe GET /v1/charges", "count": 1}]
    assert r.db_samples and set(r.phases) >= {"build", "services", "migrate", "seed", "start"}
    import psycopg

    with psycopg.connect(PG_URL) as c:  # the ephemeral database was dropped
        assert c.execute("select count(*) from pg_database where datname = %s", (sbx.db_name,)).fetchone()[0] == 0


async def test_process_sandbox_reports_failing_start(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "boom.py").write_text("raise SystemExit(3)\n")
    plan = {
        "strategy": "template",
        "language": "python",
        "root": "",
        "start": ["python", "boom.py"],
        "health_path": "/",
        "install": [],
        "build": [],
    }
    sbx = ProcessSandbox(
        Config(env="test", sandbox_inherit_site_packages=True), "job-2", src, plan, {}, None, tmp_path / "s.log"
    )
    await sbx.build()
    with pytest.raises(SandboxError) as exc:
        await sbx.start()
    await sbx.stop()
    assert exc.value.phase == "start" and "exited with code 3" in exc.value.message


async def test_process_sandbox_requires_database_config(tmp_path: Path) -> None:
    plan = {"strategy": "template", "root": "", "services": {"postgres": {}}, "start": [sys.executable]}
    sbx = ProcessSandbox(Config(env="test"), "job-3", tmp_path, plan, {}, None, tmp_path / "s.log")
    with pytest.raises(SandboxError, match="RA_SANDBOX_POSTGRES_URL"):
        await sbx.start()
    await sbx.stop()


def test_plan_root_cannot_escape(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        ProcessSandbox(Config(env="test"), "j", tmp_path, {"root": "../.."}, {}, None, tmp_path / "s.log")


def test_json_report_roundtrip(tmp_path: Path) -> None:
    sbx = ProcessSandbox(Config(env="test"), "j", tmp_path, {"root": ""}, {}, None, tmp_path / "s.log")
    assert json.loads(json.dumps(sbx.report.to_dict()))["driver"] == "process"


# ------------------------------------------------------------- repair checks ----
def test_only_allowlisted_commands_run() -> None:
    from runner_agent.sandbox_env.base import allowed_command

    assert allowed_command(["python", "-m", "pytest", "-q", "tests/test_x.py"])
    assert allowed_command(["npm", "test", "--silent"])
    for argv in ([], ["bash", "-c", "curl evil"], ["sh", "x"], ["/usr/bin/python", "x"], ["git", "push"]):
        assert not allowed_command(argv)


async def test_process_sandbox_exec_runs_in_app_env(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "tests").mkdir(parents=True)
    (src / "tests" / "check.py").write_text("import os, sys\nprint('token', os.environ['API_TOKEN'])\nsys.exit(3)\n")
    sbx = ProcessSandbox(Config(env="test"), "job-exec1", src, {"env": {}}, {"API_TOKEN": "t0k"}, None, tmp_path / "l")
    res = await sbx.exec(["python", "tests/check.py"], 30)
    assert res.exit_code == 3 and "token t0k" in res.output and not res.timed_out
    slow = await sbx.exec(["python", "-c", "import time; time.sleep(5)"], 0.5)
    assert slow.timed_out and slow.exit_code == 124


async def test_scan_tree_normalises_installed_scanners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner_agent.scan import scan_tree

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    semgrep = {"results": [{"check_id": "python.sqli", "path": "app/db.py", "extra": {"severity": "ERROR"}}]}
    gitleaks = [{"RuleID": "aws-access-key", "File": "app/settings.py"}]
    for name, out in (("semgrep", semgrep), ("gitleaks", gitleaks)):
        exe = bin_dir / name
        exe.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({out!r}))\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    report = await scan_tree(tmp_path)
    assert report["tools"] == ["semgrep", "gitleaks"] and report["missing"] == ["osv-scanner"]
    assert {(f["tool"], f["rule"], f["severity"]) for f in report["findings"]} == {
        ("semgrep", "python.sqli", "high"),
        ("gitleaks", "aws-access-key", "high"),
    }
