"""Docker + gVisor driver (ADR-0009) with per-run network and egress allowlist (ADR-0010).

Each job gets an ``--internal`` Docker network (no route out) plus an egress proxy container
(Smokescreen) that is the only path to the internet and only allows the job's verified
hosts; Smokescreen also refuses private/link-local/metadata addresses. Test containers run
under ``runsc`` with no capabilities, a read-only root filesystem, and CPU/memory/PID limits.
Secrets are passed with an ``--env-file`` (0600, deleted on teardown) so they never appear in
process listings.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from runner_agent.config import Config
from runner_agent.drivers.base import CommandSpec, Egress

PROXY_PORT = 4750


def network_name(job_id: str) -> str:
    return f"re-run-{job_id[:12]}"


def proxy_name(job_id: str) -> str:
    return f"re-egress-{job_id[:12]}"


def acl_yaml(egress: Egress) -> str:
    hosts = "\n".join(f"    - {json.dumps(h)}" for h in sorted(set(egress.allow_hosts))) or "    []"
    return (
        "---\nversion: v1\nservices: []\ndefault:\n  name: default\n  project: reliability\n  action: enforce\n"
        f"  allowed_domains:\n{hosts}\n"
    )


def proxy_args(config: Config, job_id: str, acl_path: Path) -> list[str]:
    return [
        "run",
        "-d",
        "--rm",
        "--name",
        proxy_name(job_id),
        "--network",
        "bridge",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--memory",
        "256m",
        "-v",
        f"{acl_path}:/etc/smokescreen/acl.yaml:ro",
        config.egress_proxy_image,
        "--listen-ip",
        "0.0.0.0",  # noqa: S104 - inside the proxy container
        "--listen-port",
        str(PROXY_PORT),
        "--egress-acl-file",
        "/etc/smokescreen/acl.yaml",
    ]


def run_args(config: Config, job_id: str, spec: CommandSpec, env_file: Path | None) -> list[str]:
    image = config.k6_image if spec.tool == "k6" else config.playwright_image
    proxy = f"http://{proxy_name(job_id)}:{PROXY_PORT}"
    args = [
        "run",
        "--rm",
        "--name",
        f"re-{job_id[:12]}-{spec.tool}",
        "--runtime",
        config.docker_runtime,
        "--network",
        network_name(job_id),
        "--cpus",
        str(spec.limits.cpu),
        "--memory",
        f"{spec.limits.mem_gb}g",
        "--pids-limit",
        str(spec.limits.pids),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=512m",
        "--user",
        "10001:10001",
        "-v",
        f"{spec.workdir}:/work:rw",
        "-w",
        "/work",
        "-e",
        f"HTTPS_PROXY={proxy}",
        "-e",
        f"HTTP_PROXY={proxy}",
    ]
    for key, value in sorted(spec.env.items()):
        args += ["-e", f"{key}={value}"]
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    if spec.tool == "k6":
        args += [image, *spec.args]
    elif spec.tool == "explorer":
        args += [image, "node", "/opt/re/explore.cjs", *spec.args]
    else:
        args += [image, "node", "/opt/re/node_modules/@playwright/test/cli.js", *spec.args]
    return args


class DockerHandle:
    def __init__(self, driver: DockerDriver, name: str, proc: asyncio.subprocess.Process) -> None:
        self._driver, self.name, self.proc = driver, name, proc
        self._cpu: float | None = None

    async def wait(self) -> int:
        return await self.proc.wait()

    async def stop(self, grace_s: float = 5.0) -> None:
        await self._driver.docker("stop", "-t", str(int(grace_s)), self.name, check=False)

    def cpu_fraction(self) -> float | None:
        return self._cpu

    async def sample_cpu(self, cpus: float) -> None:
        out = await self._driver.docker("stats", "--no-stream", "--format", "{{.CPUPerc}}", self.name, check=False)
        try:
            self._cpu = round(float(out.strip().rstrip("%")) / (100 * cpus), 3)
        except ValueError:
            pass


class DockerDriver:
    name = "docker"

    def __init__(self, config: Config) -> None:
        self.config = config

    async def docker(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.config.docker_binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {err.decode()[:300]}")
        return out.decode()

    async def prepare(self, job_id: str, egress: Egress) -> None:
        workdir = Path(self.config.workdir).resolve() / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        acl = workdir / "egress-acl.yaml"
        acl.write_text(acl_yaml(egress))
        await self.docker("network", "create", "--internal", network_name(job_id))
        await self.docker(*proxy_args(self.config, job_id, acl))
        await self.docker("network", "connect", network_name(job_id), proxy_name(job_id))

    async def start(self, job_id: str, spec: CommandSpec) -> DockerHandle:
        env_file = None
        if spec.secret_env:
            env_file = spec.workdir / ".secrets.env"
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                for key, value in spec.secret_env.items():
                    fh.write(f"{key}={value}\n")
        args = run_args(self.config, job_id, spec, env_file)
        log = open(spec.workdir / f"{spec.tool}.log", "ab")  # noqa: SIM115
        proc = await asyncio.create_subprocess_exec(self.config.docker_binary, *args, stdout=log, stderr=log)
        handle = DockerHandle(self, args[args.index("--name") + 1], proc)
        return handle

    async def teardown(self, job_id: str) -> None:
        await self.docker("rm", "-f", proxy_name(job_id), check=False)
        await self.docker("network", "rm", network_name(job_id), check=False)
        secrets = Path(self.config.workdir).resolve() / job_id / ".secrets.env"
        secrets.unlink(missing_ok=True)
