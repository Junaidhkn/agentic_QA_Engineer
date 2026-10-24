"""Docker sandbox: build images with BuildKit and run the app as a compose project under gVisor
on the job's internal network (ADR-0009, ADR-0011).

Builds run in a per-job BuildKit container attached to a build network whose only way out is
an egress proxy allowing the package mirror hosts (threat model A6); layer caches are kept per
project on the host. The load generator joins the run network and reaches the app at
``http://app:<port>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import yaml

from runner_agent.config import Config
from runner_agent.sandbox_env import compose as compose_mod
from runner_agent.sandbox_env.base import (
    CONNECTIONS_SQL,
    DB_STATS_SQL,
    STATEMENTS_SQL,
    Phase,
    SandboxError,
    SandboxReport,
)

BUILD_PROXY_PORT = 4750


def builder_name(job_id: str) -> str:
    return f"re-build-{job_id[:12]}"


def build_network(job_id: str) -> str:
    return f"re-build-{job_id[:12]}"


def image_tag(job_id: str) -> str:
    return f"re-sbx/{job_id[:12]}:app"


def buildx_create_args(config: Config, job_id: str) -> list[str]:
    proxy = f"http://re-build-egress-{job_id[:12]}:{BUILD_PROXY_PORT}"
    return [
        "buildx",
        "create",
        "--name",
        builder_name(job_id),
        "--driver",
        "docker-container",
        "--driver-opt",
        f"network={build_network(job_id)}",
        "--driver-opt",
        f"env.http_proxy={proxy}",
        "--driver-opt",
        f"env.https_proxy={proxy}",
    ]


def buildx_build_args(config: Config, job_id: str, context: Path, dockerfile: Path, project_id: str) -> list[str]:
    cache = Path(config.build_cache_dir) / project_id
    proxy = f"http://re-build-egress-{job_id[:12]}:{BUILD_PROXY_PORT}"
    args = [
        "buildx",
        "build",
        "--builder",
        builder_name(job_id),
        "--load",
        "--tag",
        image_tag(job_id),
        "--file",
        str(dockerfile),
        "--build-arg",
        f"OTEL_IMAGE={config.otel_autoinstrument_image}",
        "--build-arg",
        f"HTTP_PROXY={proxy}",
        "--build-arg",
        f"HTTPS_PROXY={proxy}",
        "--cache-from",
        f"type=local,src={cache}",
        "--cache-to",
        f"type=local,dest={cache},mode=max",
        "--provenance=false",
    ]
    if config.pip_index_url:
        args += ["--build-arg", f"PIP_INDEX_URL={config.pip_index_url}"]
    if config.npm_registry:
        args += ["--build-arg", f"NPM_CONFIG_REGISTRY={config.npm_registry}"]
    return [*args, str(context)]


def compose_args(job_id: str, compose_file: Path, *rest: str) -> list[str]:
    # The project directory is the parent of .re-sandbox/, so relative paths (build contexts,
    # sidecar config mounts) resolve against the customer's compose directory / source root.
    return [
        "compose",
        "-p",
        compose_mod.project_name(job_id),
        "--project-directory",
        str(compose_file.parent.parent),
        "-f",
        str(compose_file),
        *rest,
    ]


class DockerSandbox:
    def __init__(
        self,
        config: Config,
        job_id: str,
        src: Path,
        plan: dict[str, Any],
        app_env: dict[str, str],
        otlp: dict[str, str] | None,
        log_path: Path,
        project_id: str,
        run_id: str | None = None,
    ) -> None:
        self.config, self.job_id, self.src, self.plan = config, job_id, src.resolve(), plan
        self.app_dir = (self.src / plan.get("root", "")).resolve()
        if self.src not in [self.app_dir, *self.app_dir.parents]:
            raise SandboxError("plan", "app root escapes the source tree")
        self.app_env, self.otlp, self.log_path = app_env, otlp or {}, log_path
        self.project_id, self.run_id = project_id, run_id
        self.report = SandboxReport(
            strategy=plan.get("strategy", "template"),
            driver="docker",
            placeholders=list(plan.get("placeholders", [])),
            warnings=list(plan.get("warnings", [])),
            instrumentation="auto",
        )
        project_dir = (self.src / plan["compose_file"]).parent if plan.get("strategy") == "compose" else self.src
        if self.src not in [project_dir.resolve(), *project_dir.resolve().parents]:
            raise SandboxError("plan", "compose file escapes the source tree")
        self.sandbox_dir = project_dir / ".re-sandbox"
        self.compose_file = self.sandbox_dir / "compose.yaml"
        self.pg_password = secrets.token_urlsafe(18)
        self._log = log_path.open("ab")

    async def _docker(self, phase: str, args: list[str], limit_s: float = 900, check: bool = True) -> str:
        self._log.write(f"\n$ docker {' '.join(args)}\n".encode())
        proc = await asyncio.create_subprocess_exec(
            self.config.docker_binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": os.environ.get("HOME", "/tmp"),
                RE_PG: self.pg_password,
                "DOCKER_BUILDKIT": "1",
            },
            cwd=self.src,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError as exc:
            proc.kill()
            raise SandboxError(phase, f"docker {args[0]} timed out") from exc
        text = out.decode(errors="replace")
        self._log.write(text[-200_000:].encode())
        if check and proc.returncode:
            raise SandboxError(phase, f"docker {' '.join(args[:2])} exited {proc.returncode}: {text[-600:]}")
        return text

    def _images(self) -> dict[str, str]:
        return {
            "postgres": self.config.sandbox_postgres_image,
            "redis": self.config.sandbox_redis_image,
            "stub": self.config.runner_image,
            "collector": self.config.otel_collector_image,
        }

    def _project_limits(self) -> dict[str, Any]:
        return {"cpus": "2", "mem_limit": "4g", "pids_limit": 2048}

    async def build(self) -> None:
        sandbox_dir = self.sandbox_dir
        sandbox_dir.mkdir(exist_ok=True)
        env_file = sandbox_dir / "app.env"
        env_file.touch(mode=0o600)
        env_file.write_text("".join(f"{k}={v}\n" for k, v in self.app_env.items() if "\n" not in v))
        (sandbox_dir / "stubs.json").write_text(json.dumps(self.plan.get("stubs", [])))
        (sandbox_dir / "collector.yaml").write_text(
            compose_mod.collector_config(
                self.otlp.get("endpoint", "http://localhost:4319"), self.otlp.get("token", ""), None
            )
        )
        with Phase(self.report, "build"):
            strategy = self.plan.get("strategy")
            if strategy == "compose":
                doc = yaml.safe_load((self.src / self.plan["compose_file"]).read_text()) or {}
                try:
                    project = compose_mod.rewrite_compose(
                        doc,
                        self.plan,
                        self.job_id,
                        self.config.docker_runtime,
                        self._images(),
                        self.src,
                        self.run_id,
                        self._project_limits(),
                    )
                except compose_mod.ComposeError as exc:
                    raise SandboxError("build", str(exc)) from exc
                self._write_compose(project)
                await self._docker("build", compose_args(self.job_id, self.compose_file, "build"), limit_s=1200)
                return
            dockerfile = self.src / self.plan["dockerfile"] if strategy == "dockerfile" else sandbox_dir / "Dockerfile"
            if strategy != "dockerfile":
                dockerfile.write_text(compose_mod.render_dockerfile(self.plan))
                # Dockerfile-specific ignore file: platform files (incl. app.env) never enter the image.
                ignore = (
                    (self.app_dir / ".dockerignore").read_text() if (self.app_dir / ".dockerignore").exists() else ""
                )
                (sandbox_dir / "Dockerfile.dockerignore").write_text(
                    ignore + "\n.re-sandbox\n.git\nnode_modules\n.env*\n"
                )
            await self._docker("build", buildx_create_args(self.config, self.job_id))
            await self._docker(
                "build",
                buildx_build_args(self.config, self.job_id, self.app_dir, dockerfile, self.project_id),
                limit_s=1200,
            )
            self._write_compose(
                compose_mod.generate_compose(
                    self.plan,
                    self.job_id,
                    self.config.docker_runtime,
                    self._images(),
                    image_tag(self.job_id),
                    self.run_id,
                )
            )

    def _write_compose(self, project: dict[str, Any]) -> None:
        app = project["services"][self._app()]
        app["env_file"] = ["./.re-sandbox/app.env"]  # the app's own SANDBOX_* secrets, never in the compose file
        self.compose_file.write_text(yaml.safe_dump(project, sort_keys=False))

    def _app(self) -> str:
        return (self.plan.get("app_service") or "app") if self.plan.get("strategy") == "compose" else "app"

    async def start(self) -> str:
        with Phase(self.report, "services"):
            deps = ["postgres", "redis", "stub", "otel-collector"]
            await self._docker(
                "services",
                compose_args(
                    self.job_id, self.compose_file, "up", "-d", "--wait", "--wait-timeout", "180", *self._existing(deps)
                ),
            )
            self.report.services = self._existing(deps)
        for phase, cmds in (("migrate", self.plan.get("migrate") or []), ("seed", self.plan.get("seed") or [])):
            if not cmds:
                continue
            with Phase(self.report, phase):
                try:
                    for cmd in cmds:
                        await self._docker(
                            phase,
                            compose_args(self.job_id, self.compose_file, "run", "--rm", "--no-deps", self._app(), *cmd),
                        )
                except SandboxError:
                    setattr(self.report, "migrations" if phase == "migrate" else "seed", "failed")
                    raise
                setattr(self.report, "migrations" if phase == "migrate" else "seed", "applied")
        with Phase(self.report, "start"):
            await self._docker(
                "start",
                compose_args(
                    self.job_id, self.compose_file, "up", "-d", "--wait", "--wait-timeout", "180", self._app()
                ),
            )
            port = int(self.plan.get("port") or 8000)
            base = f"http://{self._app()}:{port}"
            await self._wait_healthy(base + (self.plan.get("health_path") or "/"))
        self.report.healthy, self.report.base_url = True, base
        return base

    def _existing(self, names: list[str]) -> list[str]:
        doc = yaml.safe_load(self.compose_file.read_text()) or {}
        return [n for n in names if n in (doc.get("services") or {})]

    async def _wait_healthy(self, url: str) -> None:
        """Probe from inside the run network with a throwaway curl container."""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            out = await self._docker(
                "health",
                [
                    "run",
                    "--rm",
                    "--network",
                    compose_mod.network_name(self.job_id),
                    "--runtime",
                    self.config.docker_runtime,
                    self.config.curl_image,
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--max-time",
                    "2",
                    url,
                ],
                limit_s=30,
                check=False,
            )
            code = out.strip()[-3:]
            if code.isdigit() and 0 < int(code) < 500:
                return
            await asyncio.sleep(1.0)
        raise SandboxError("health", f"no healthy response from {url} within 120s")

    async def _psql(self, sql: str) -> Any:
        pg = (self.plan.get("services") or {}).get("postgres") or {}
        service = pg.get("from_compose") or "postgres"
        out = await self._docker(
            "evidence",
            compose_args(
                self.job_id,
                self.compose_file,
                "exec",
                "-T",
                service,
                "psql",
                "-U",
                pg.get("user", "sandbox"),
                "-d",
                pg.get("database", "app"),
                "-At",
                "-c",
                sql,
            ),
            limit_s=30,
        )
        return json.loads(out.strip().splitlines()[-1]) if out.strip() else None

    async def sample(self) -> None:
        if "postgres" not in (self.plan.get("services") or {}):
            return
        with contextlib.suppress(Exception):
            self.report.db_samples.append({"t": round(time.time(), 1), **(await self._psql(CONNECTIONS_SQL) or {})})

    async def collect(self) -> None:
        if "postgres" not in (self.plan.get("services") or {}):
            return
        try:
            stats = await self._psql(DB_STATS_SQL) or {}
            with contextlib.suppress(Exception):
                await self._psql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
                stats["statements"] = await self._psql(STATEMENTS_SQL)
            self.report.db = stats
        except Exception as exc:
            self.report.warnings.append(f"database statistics unavailable: {type(exc).__name__}")

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self._docker(
                "teardown",
                compose_args(self.job_id, self.compose_file, "down", "-v", "--remove-orphans", "--timeout", "5"),
                check=False,
            )
            await self._docker("teardown", ["buildx", "rm", "--force", builder_name(self.job_id)], check=False)
            await self._docker("teardown", ["image", "rm", "-f", image_tag(self.job_id)], check=False)
        self._log.close()


RE_PG = compose_mod.PG_PASSWORD_ENV
