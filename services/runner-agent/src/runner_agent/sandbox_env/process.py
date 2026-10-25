"""Process sandbox: runs the app-under-test directly on the host. Local development and tests
only (no isolation — the runner refuses it outside local/test, like the process driver).

Python apps get a virtualenv (optionally inheriting the runner interpreter's site-packages so
installs resolve offline); Node apps use the host's npm. Postgres is an ephemeral database
created on ``RA_SANDBOX_POSTGRES_URL`` and dropped on teardown; Redis uses
``RA_SANDBOX_REDIS_URL``. External dependencies are pointed at an in-process stub server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import site
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from runner_agent.config import Config
from runner_agent.sandbox_env.base import (
    CONNECTIONS_SQL,
    DB_STATS_SQL,
    EXEC_OUTPUT_LIMIT,
    STATEMENTS_SQL,
    ExecResult,
    Phase,
    SandboxError,
    SandboxReport,
    fill,
)
from runner_agent.sandbox_env.stub import StubServer

HEALTH_TIMEOUT_S = 120
STEP_TIMEOUT_S = 900
PLACEHOLDER = "sandbox-placeholder"
# Host settings builds need to reach the package mirror (the process sandbox is local/test only;
# Docker sandboxes get an explicit proxy to the mirror instead).
PASSTHROUGH_ENV = {
    "PATH",
    "HOME",
    "LANG",
    "TMPDIR",
    "SYSTEMROOT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
    "NPM_CONFIG_CAFILE",
    "npm_config_cafile",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def database_url(admin_url: str, database: str, scheme: str) -> str:
    parts = urlsplit(admin_url)
    return urlunsplit((scheme, parts.netloc, f"/{database}", parts.query, ""))


class ProcessSandbox:
    def __init__(
        self,
        config: Config,
        job_id: str,
        src: Path,
        plan: dict[str, Any],
        app_env: dict[str, str],
        otlp: dict[str, str] | None,
        log_path: Path,
        run_id: str | None = None,
    ) -> None:
        self.config, self.job_id, self.plan = config, job_id, plan
        self.app_dir = (src / plan.get("root", "")).resolve()
        if src.resolve() not in [self.app_dir, *self.app_dir.parents]:
            raise SandboxError("plan", "app root escapes the source tree")
        self.source_dir = self.app_dir
        self.app_env, self.otlp, self.log_path, self.run_id = app_env, otlp or {}, log_path, run_id
        self.report = SandboxReport(
            strategy=plan.get("strategy", "template"),
            driver="process",
            placeholders=list(plan.get("placeholders", [])),
            warnings=list(plan.get("warnings", [])),
        )
        self.port = free_port()
        self.venv: Path | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.stub: StubServer | None = None
        self.db_name: str | None = None
        self.db_url: str | None = None
        self._log = log_path.open("ab")

    # ----------------------------------------------------------- helpers ----
    def _write(self, text: str) -> None:
        self._log.write(text.encode(errors="replace"))
        self._log.flush()

    def _base_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in PASSTHROUGH_ENV}
        env.update({"CI": "1", "PYTHONUNBUFFERED": "1"})  # NODE_ENV only at runtime: installs need devDependencies
        if self.venv is not None:
            env["PATH"] = f"{self.venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(self.venv)
        if self.config.pip_index_url:
            env["PIP_INDEX_URL"] = self.config.pip_index_url
        if self.config.npm_registry:
            env["NPM_CONFIG_REGISTRY"] = self.config.npm_registry
        return env

    def _app_env(self) -> dict[str, str]:
        env = self._base_env()
        for name in self.plan.get("placeholders", []):
            env[name] = PLACEHOLDER
        env.update({k: fill(v, self.port) for k, v in (self.plan.get("env") or {}).items()})
        env.update(self.app_env)
        env["PORT"] = str(self.port)
        if self.db_url:
            for name in self.plan.get("database_env") or ["DATABASE_URL"]:
                env[name] = self.db_url
        if "redis" in (self.plan.get("services") or {}) and self.config.sandbox_redis_url:
            env["REDIS_URL"] = self.config.sandbox_redis_url
        if self.stub is not None:
            for stub in self.plan.get("stubs", []):
                env[stub["env"]] = f"http://127.0.0.1:{self.stub.port}/{stub['name']}"
        if self.otlp.get("endpoint"):
            env.update(
                {
                    "OTEL_SERVICE_NAME": "app",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": self.otlp["endpoint"],
                    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                    "OTEL_EXPORTER_OTLP_HEADERS": f"authorization=Bearer {self.otlp.get('token', '')}",
                    "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment=sandbox,re.run_id={self.run_id or ''}",
                }
            )
        return env

    def _argv(self, cmd: list[str]) -> list[str]:
        argv = [fill(a, self.port) for a in cmd]
        if self.venv is not None and argv and argv[0] in ("python", "pip", "pytest", "alembic", "uvicorn", "gunicorn"):
            exe = self.venv / "bin" / argv[0]
            if exe.exists():
                argv[0] = str(exe)
        return argv

    async def _run(self, phase: str, cmd: list[str], env: dict[str, str]) -> None:
        argv = self._argv(cmd)
        self._write(f"\n$ {' '.join(argv)}\n")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.app_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), STEP_TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise SandboxError(phase, f"{' '.join(cmd[:3])} timed out") from exc
        self._write(out.decode(errors="replace")[-200_000:])
        if proc.returncode:
            tail = out.decode(errors="replace").strip().splitlines()[-5:]
            raise SandboxError(phase, f"`{' '.join(cmd[:4])}` exited {proc.returncode}: {' | '.join(tail)[-600:]}")

    # ------------------------------------------------------------- build ----
    async def build(self) -> None:
        with Phase(self.report, "build"):
            if self.plan.get("language") == "python" or self.plan.get("template") == "python":
                self.venv = self.app_dir / ".re-venv"
                args = [
                    sys.executable,
                    "-m",
                    "venv",
                    "--without-pip" if self.config.sandbox_inherit_site_packages else "--upgrade-deps",
                    str(self.venv),
                ]
                await self._run("build", args, self._base_env())
                if self.config.sandbox_inherit_site_packages:
                    self._inherit_site_packages()
            env = self._base_env()
            for cmd in self.plan.get("install", []):
                if (
                    self.venv is not None
                    and self.config.sandbox_inherit_site_packages
                    and cmd[:2] == ["pip", "install"]
                ):
                    if await self._satisfied(cmd[2:]):
                        self._write(f"\n# {' '.join(cmd)}: satisfied by inherited packages\n")
                        continue
                    cmd = self._installer(cmd[2:])
                await self._run("build", cmd, env)
            for cmd in self.plan.get("build", []):
                await self._run("build", cmd, env)

    async def _satisfied(self, args: list[str]) -> bool:
        """Dev/test: are all requirements already importable in the venv (inherited packages)?"""
        assert self.venv is not None
        reqs: list[str] = []
        it = iter(a for a in args if a != "--no-cache-dir")
        for a in it:
            if a == "-r":
                path = (self.app_dir / next(it, "")).resolve()
                if self.app_dir not in path.parents:
                    return False
                reqs += [
                    ln.split("#")[0].strip()
                    for ln in path.read_text().splitlines()
                    if ln.strip() and not ln.strip().startswith(("#", "-"))
                ]
            elif not a.startswith("-"):
                reqs.append(a)
        check = (
            "import sys, importlib.metadata as m, re\n"
            "missing = []\n"
            "for r in sys.argv[1:]:\n"
            "    name = re.split(r'[\\[<>=!~; ]', r, maxsplit=1)[0]\n"
            "    try: m.version(name)\n"
            "    except m.PackageNotFoundError: missing.append(name)\n"
            "print(','.join(missing)); sys.exit(1 if missing else 0)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            str(self.venv / "bin" / "python"),
            "-c",
            check,
            *reqs,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode:
            self._write(f"\n# not satisfied by inherited packages: {out.decode(errors='replace').strip()}\n")
        return proc.returncode == 0

    def _installer(self, args: list[str]) -> list[str]:
        assert self.venv is not None
        target = str(self.venv / "bin" / "python")
        uv = shutil.which("uv")
        if uv:
            return [uv, "pip", "install", "--python", target, *args]
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--python", target, *args]

    def _inherit_site_packages(self) -> None:
        """Dev/test: make the runner's installed packages visible so installs resolve offline."""
        assert self.venv is not None
        paths = [p for p in [*site.getsitepackages(), *sys.path] if p.endswith("site-packages") and os.path.isdir(p)]
        for lib in (self.venv / "lib").glob("python*/site-packages"):
            (lib / "_re_inherit.pth").write_text("\n".join(dict.fromkeys(paths)) + "\n")

    # ------------------------------------------------------------- start ----
    async def _create_database(self) -> None:
        if "postgres" not in (self.plan.get("services") or {}):
            return
        admin = self.config.sandbox_postgres_url
        if not admin:
            raise SandboxError("services", "the app needs PostgreSQL but RA_SANDBOX_POSTGRES_URL is not configured")
        import psycopg

        self.db_name = f"sbx_{self.job_id.replace('-', '')[:20]}"
        async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
            await conn.execute(f'DROP DATABASE IF EXISTS "{self.db_name}"')
            await conn.execute(f'CREATE DATABASE "{self.db_name}"')
        self.db_url = database_url(admin, self.db_name, self.plan.get("database_scheme") or "postgresql")
        self.report.services.append("postgres")
        with contextlib.suppress(Exception):
            async with await psycopg.AsyncConnection.connect(self._plain_db_url(), autocommit=True) as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    def _plain_db_url(self) -> str:
        assert self.db_name is not None
        return database_url(self.config.sandbox_postgres_url or "", self.db_name, "postgresql")

    async def start(self) -> str:
        with Phase(self.report, "services"):
            await self._create_database()
            if "redis" in (self.plan.get("services") or {}):
                if not self.config.sandbox_redis_url:
                    raise SandboxError("services", "the app needs Redis but RA_SANDBOX_REDIS_URL is not configured")
                self.report.services.append("redis")
            if self.plan.get("stubs"):
                self.stub = StubServer(self.plan["stubs"]).start()
                self.report.services.append("stub")
        env = self._app_env()
        if self.plan.get("migrate"):
            with Phase(self.report, "migrate"):
                try:
                    for cmd in self.plan["migrate"]:
                        await self._run("migrate", cmd, env)
                except SandboxError:
                    self.report.migrations = "failed"
                    raise
                self.report.migrations = "applied"
        if self.plan.get("seed"):
            with Phase(self.report, "seed"):
                try:
                    for cmd in self.plan["seed"]:
                        await self._run("seed", cmd, env)
                except SandboxError:
                    self.report.seed = "failed"
                    raise
                self.report.seed = "applied"
        start = list(self.plan.get("start") or [])
        if not start:
            raise SandboxError("start", "the plan has no start command")
        if self.venv is not None and (self.venv / "bin" / "opentelemetry-instrument").exists() and self.otlp:
            start = [str(self.venv / "bin" / "opentelemetry-instrument"), *start]
            self.report.instrumentation = "python-auto"
        elif (self.app_dir / "node_modules/@opentelemetry/auto-instrumentations-node").is_dir() and self.otlp:
            env["NODE_OPTIONS"] = "--require @opentelemetry/auto-instrumentations-node/register"
            self.report.instrumentation = "node-auto"
        with Phase(self.report, "start"):
            argv = self._argv(start)
            self._write(f"\n$ {' '.join(argv)}\n")
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.app_dir,
                env=env,
                stdout=self._log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            base = f"http://127.0.0.1:{self.port}"
            await self._wait_healthy(base + (self.plan.get("health_path") or "/"))
        self.report.healthy, self.report.base_url = True, base
        return base

    async def _wait_healthy(self, url: str) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.proc is not None and self.proc.returncode is not None:
                    raise SandboxError("start", f"the app exited with code {self.proc.returncode} before serving")
                try:
                    if (await client.get(url)).status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise SandboxError("health", f"no healthy response from {url} within {HEALTH_TIMEOUT_S}s")

    # -------------------------------------------------------------- exec ----
    async def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        """Run an allowlisted command in the app directory with the app's environment."""
        cmd = self._argv(argv)
        self._write(f"\n$ {' '.join(cmd)}\n")
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.app_dir,
            env={**self._app_env(), "BASE_URL": self.report.base_url or ""},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        self._write(text[-200_000:])
        return ExecResult(
            proc.returncode if proc.returncode is not None and not timed_out else 124,
            text[-EXEC_OUTPUT_LIMIT:],
            round(time.monotonic() - started, 2),
            timed_out,
        )

    # ----------------------------------------------------------- evidence ----
    async def _query(self, sql: str) -> Any:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._plain_db_url(), autocommit=True) as conn:
            cur = await conn.execute(sql)
            row = await cur.fetchone()
            return row[0] if row else None

    async def sample(self) -> None:
        if self.db_name is None:
            return
        with contextlib.suppress(Exception):
            value = await self._query(CONNECTIONS_SQL)
            self.report.db_samples.append({"t": round(time.time(), 1), **(value or {})})

    async def collect(self) -> None:
        if self.stub is not None:
            self.report.stub_calls = self.stub.summary()
        if self.db_name is None:
            return
        try:
            stats = await self._query(DB_STATS_SQL) or {}
        except Exception as exc:  # evidence is best-effort
            self.report.warnings.append(f"database statistics unavailable: {type(exc).__name__}")
            return
        with contextlib.suppress(Exception):
            stats["statements"] = await self._query(STATEMENTS_SQL)
        self.report.db = stats

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        if self.stub is not None:
            self.stub.stop()
        if self.db_name and self.config.sandbox_postgres_url:
            import psycopg

            with contextlib.suppress(Exception):
                async with await psycopg.AsyncConnection.connect(
                    self.config.sandbox_postgres_url, autocommit=True
                ) as conn:
                    await conn.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        self._log.close()

    def describe(self) -> str:
        return json.dumps({"driver": "process", "port": self.port})
