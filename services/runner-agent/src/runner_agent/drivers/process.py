"""Process driver — runs k6/Playwright as local subprocesses.

DEVELOPMENT ONLY: no sandbox, no egress enforcement. ``Config`` refuses this driver outside
local/test. Secrets are passed through the child environment only.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import psutil

from runner_agent.config import Config
from runner_agent.drivers.base import CommandSpec, Egress

PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "PLAYWRIGHT_BROWSERS_PATH",
    "LANG",
    "TMPDIR",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)


class ProcessHandle:
    def __init__(self, proc: asyncio.subprocess.Process, cpus: float) -> None:
        self.proc = proc
        self._cpus = cpus
        try:
            self._ps: psutil.Process | None = psutil.Process(proc.pid)
            self._ps.cpu_percent(None)
        except psutil.Error:
            self._ps = None

    async def wait(self) -> int:
        return await self.proc.wait()

    async def stop(self, grace_s: float = 5.0) -> None:
        if self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            await asyncio.wait_for(self.proc.wait(), grace_s)
        except (TimeoutError, ProcessLookupError):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cpu_fraction(self) -> float | None:
        if self._ps is None:
            return None
        try:
            total = self._ps.cpu_percent(None) + sum(c.cpu_percent(None) for c in self._ps.children(recursive=True))
        except psutil.Error:
            return None
        return round(total / (100 * self._cpus), 3)


class ProcessDriver:
    name = "process"

    def __init__(self, config: Config) -> None:
        self.config = config

    async def prepare(self, job_id: str, egress: Egress) -> None:
        return None

    async def start(self, job_id: str, spec: CommandSpec) -> ProcessHandle:
        if spec.tool == "k6":
            argv = [self.config.k6_binary, *spec.args]
        elif spec.tool == "playwright":
            pw_dir = Path(self.config.playwright_dir).resolve()
            link = spec.workdir / "node_modules"
            if not link.exists():
                link.symlink_to(pw_dir / "node_modules", target_is_directory=True)
            argv = ["node", str(pw_dir / "node_modules" / "@playwright" / "test" / "cli.js"), *spec.args]
        else:
            raise ValueError(f"unknown tool {spec.tool}")
        env = {k: v for k, v in os.environ.items() if k in PASSTHROUGH_ENV}
        env.update(spec.env)
        env.update(spec.secret_env)
        log = open(spec.workdir / f"{spec.tool}.log", "ab")  # noqa: SIM115 - closed when process exits
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=spec.workdir, env=env, stdout=log, stderr=log, start_new_session=True
        )
        return ProcessHandle(proc, spec.limits.cpu)

    async def teardown(self, job_id: str) -> None:
        return None
