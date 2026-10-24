"""Executes one lease: fetch bundles → run tools under the driver → stream progress →
honour cancel / abort / wall-clock limits → upload artifacts → report completion."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import logging
import math
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runner_agent.client import LeaseClient
from runner_agent.config import Config
from runner_agent.drivers.base import CommandSpec, Driver, Egress, Handle, Limits
from runner_agent.k6stream import K6Aggregator
from runner_agent.keys import RunnerKeys
from runner_agent.sandbox_env.base import Sandbox, SandboxError, allowed_command, extract_source
from runner_agent.sandbox_env.docker import DockerSandbox
from runner_agent.sandbox_env.process import ProcessSandbox
from runner_agent.scan import scan_tree

log = logging.getLogger(__name__)
CONTENT_TYPES = {".json": "application/json", ".gz": "application/gzip", ".log": "text/plain"}
K6_ABORT_EXIT = 105
SANDBOX_SAMPLE_S = 5.0


@dataclass
class JobState:
    phase: str = "provisioning"
    progress: dict[str, Any] | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    abort_reason: str | None = None
    max_cpu: float = 0.0
    cpu_samples: list[float] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    exit_codes: dict[str, int] = field(default_factory=dict)
    vu_minutes: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    sandbox: Sandbox | None = None
    # NFR-7: cancel received (heartbeat reply) → tool stopped, reported to the control plane.
    cancel_seen_at: float | None = None
    stopped_at: float | None = None


def _safe_extract(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe path in bundle: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported member type in bundle: {member.name}")
        tar.extractall(dest, filter="data")


class JobRunner:
    def __init__(self, config: Config, client: LeaseClient, keys: RunnerKeys, driver: Driver) -> None:
        self.config, self.client, self.keys, self.driver = config, client, keys, driver

    async def run(self, lease: dict[str, Any]) -> None:
        lease_id = lease["lease_id"]
        workdir = Path(self.config.workdir).resolve() / lease_id
        workdir.mkdir(parents=True, exist_ok=True)
        state = JobState()
        hb = asyncio.create_task(self._heartbeats(lease, state))
        outcome, error = "succeeded", None
        sandbox: Sandbox | None = None
        sampler: asyncio.Task[None] | None = None
        sandbox_started = 0.0
        try:
            secrets = self.keys.unseal(lease["secrets_sealed"]) if lease.get("secrets_sealed") else {}
            spec_egress = lease.get("egress") or {}
            egress = Egress(spec_egress.get("allow_hosts", []), spec_egress.get("allow_ips", []))
            await self.driver.prepare(lease_id, egress)
            await self._fetch_bundles(lease, workdir)
            limits = Limits(**lease.get("limits", {}))
            env = dict(lease.get("env", {}))
            if lease.get("sandbox"):
                sandbox_started = time.monotonic()
                sandbox = await self._make_sandbox(lease, workdir)
                state.phase = "building"
                log.info("sandbox build", extra={"lease_id": lease_id})
                await sandbox.build()
                state.phase = "starting"
                log.info("sandbox start", extra={"lease_id": lease_id})
                env["BASE_URL"] = await sandbox.start()
                log.info("sandbox healthy at %s", env["BASE_URL"], extra={"lease_id": lease_id})
                sampler = asyncio.create_task(self._sample(sandbox))
                state.sandbox = sandbox
            state.phase = "running"
            await self._report_started(lease, state)
            job_type = lease["job_type"]
            work = self._dispatch(job_type, lease, workdir, env, secrets, limits, state)
            try:
                await asyncio.wait_for(work, timeout=float(lease.get("max_wall_s", 1800)))
            except TimeoutError:
                state.abort_reason = "maximum wall-clock time exceeded"
            state.phase = "uploading"
            if state.cancelled.is_set():
                outcome = "cancelled"
            elif state.abort_reason:
                outcome = "aborted"
        except SandboxError as exc:
            outcome, error = "error", f"sandbox {exc.phase}: {exc.message}"[:480]
            if sandbox is not None:
                sandbox.report.error, sandbox.report.error_phase = exc.message[:2000], exc.phase
        except Exception as exc:  # report, never crash the agent
            log.exception("job failed", extra={"lease_id": lease_id})
            outcome, error = "error", f"{type(exc).__name__}: {exc}"[:480]
        finally:
            hb.cancel()
            if sampler is not None:
                sampler.cancel()
            if sandbox is not None:
                await self._finish_sandbox(sandbox, workdir, state, sandbox_started)
            with contextlib.suppress(Exception):
                await self.driver.teardown(lease_id)
        artifacts = await self._upload(lease, workdir, state)
        usage = {"vu_minutes": state.vu_minutes}
        if sandbox_started:
            usage["sandbox_minutes"] = max(1, math.ceil((time.monotonic() - sandbox_started) / 60))
        await self.client.complete(
            lease_id,
            {
                "outcome": outcome,
                "abort_reason": error or state.abort_reason,
                "exit_codes": state.exit_codes,
                "artifacts": artifacts,
                "usage": usage,
                "generator_health": {
                    "max_cpu": round(state.max_cpu, 3),
                    "avg_cpu": round(sum(state.cpu_samples) / len(state.cpu_samples), 3) if state.cpu_samples else None,
                    "driver": self.driver.name,
                    **(
                        {"kill_latency_s": round(state.stopped_at - state.cancel_seen_at, 3)}
                        if state.cancel_seen_at is not None and state.stopped_at is not None
                        else {}
                    ),
                },
                "result": state.result,
            },
        )
        shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------ sandbox ----
    async def _make_sandbox(self, lease: dict[str, Any], workdir: Path) -> Sandbox:
        section = lease["sandbox"]
        try:
            data = await self.client.download(section["source_url"])
            src = extract_source(data, workdir / "src")
        except Exception as exc:
            raise SandboxError("source", f"could not fetch the source archive ({type(exc).__name__})") from exc
        app_env = self.keys.unseal(section["app_secrets_sealed"]) if section.get("app_secrets_sealed") else {}
        log_path = workdir / "sandbox.log"
        args = (self.config, lease["lease_id"], src, section["plan"], app_env, lease.get("otlp"), log_path)
        if self.config.driver == "docker":
            return DockerSandbox(*args, project_id=lease.get("project_id", "default"), run_id=lease.get("run_id"))
        return ProcessSandbox(*args, run_id=lease.get("run_id"))

    async def _sample(self, sandbox: Sandbox) -> None:
        while True:
            await sandbox.sample()
            await asyncio.sleep(SANDBOX_SAMPLE_S)

    async def _finish_sandbox(self, sandbox: Sandbox, workdir: Path, state: JobState, started: float) -> None:
        log.info("sandbox teardown (healthy=%s)", sandbox.report.healthy)
        with contextlib.suppress(Exception):
            if sandbox.report.healthy:
                await sandbox.collect()
        with contextlib.suppress(Exception):
            await sandbox.stop()
        sandbox.report.phases["total"] = round(time.monotonic() - started, 2)
        report_path = workdir / "sandbox.json"
        report_path.write_text(json.dumps(sandbox.report.to_dict(), default=str))
        state.result["sandbox"] = sandbox.report.to_dict()
        state.outputs["telemetry_snapshot:sandbox.json"] = report_path
        if (workdir / "sandbox.log").exists():
            state.outputs["log_bundle:sandbox.log"] = workdir / "sandbox.log"

    async def _run_sandbox_build(self, *_: Any) -> None:
        """Standalone buildability check: the sandbox was built, started and found healthy."""
        return None

    def _dispatch(
        self,
        job_type: str,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> Any:
        if job_type in ("load", "verification"):
            return self._run_load(lease, workdir, env, secrets, limits, state)
        if job_type == "functional":
            return self._run_functional(lease, workdir, env, secrets, limits, state)
        if job_type == "probe":
            return self._run_probes(lease, workdir, env, secrets, limits, state)
        if job_type == "test_validation":
            return self._run_validation(lease, workdir, env, secrets, limits, state)
        if job_type == "sandbox_build":
            return self._run_sandbox_build()
        if job_type == "repair_check":
            return self._run_checks(lease, state)
        if job_type == "ui_explore":
            return self._run_explore(lease, workdir, env, secrets, limits, state)
        raise ValueError(f"unsupported job type {job_type}")

    # ------------------------------------------------------------ helpers ----
    async def _upload(self, lease: dict[str, Any], workdir: Path, state: JobState) -> list[dict[str, Any]]:
        return await upload_outputs(self.client, lease.get("uploads") or {}, state.outputs)

    async def _fetch_bundles(self, lease: dict[str, Any], workdir: Path) -> None:
        for part, url in (lease.get("bundles") or {}).items():
            _safe_extract(await self.client.download(url), workdir / part)

    async def _report_started(self, lease: dict[str, Any], state: JobState) -> None:
        """Tell the control plane the first container is running now, not at the next periodic
        heartbeat: short jobs finish within one interval and job start (NFR-4) is measured here."""
        try:
            reply = await self.client.heartbeat(lease["lease_id"], "running", None)
        except Exception:
            log.warning("start heartbeat failed", exc_info=True)
            return
        if reply.get("action") == "cancel":
            state.cancel_seen_at = state.cancel_seen_at or time.monotonic()
            state.cancelled.set()

    async def _heartbeats(self, lease: dict[str, Any], state: JobState) -> None:
        interval = float(lease.get("heartbeat_interval_s", 3))
        while True:
            try:
                reply = await self.client.heartbeat(lease["lease_id"], state.phase, state.progress)
                if reply.get("action") == "cancel":
                    if state.cancel_seen_at is None:
                        state.cancel_seen_at = time.monotonic()
                    state.cancelled.set()
            except Exception:
                log.warning("heartbeat failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _supervise(self, handle: Handle, state: JobState, cpus: float, tick: Any | None = None) -> int:
        """Wait for the tool while sampling CPU and reacting to cancel/abort within one second."""
        waiter = asyncio.create_task(handle.wait())
        while not waiter.done():
            await asyncio.sleep(1.0)
            sampler = getattr(handle, "sample_cpu", None)
            if sampler:
                await sampler(cpus)
            cpu = handle.cpu_fraction()
            if cpu is not None:
                state.cpu_samples.append(cpu)
                state.max_cpu = max(state.max_cpu, cpu)
            if tick:
                tick()
            if state.cancelled.is_set() or state.abort_reason:
                await handle.stop()
                state.stopped_at = time.monotonic()
                break
        return await waiter

    def _spec(
        self, tool: str, args: list[str], cwd: Path, env: dict[str, str], secrets: dict[str, str], limits: Limits
    ) -> CommandSpec:
        return CommandSpec(tool=tool, args=args, workdir=cwd, env=env, secret_env=secrets, limits=limits)

    # --------------------------------------------------------------- load ----
    async def _run_load(
        self,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> None:
        k6_dir = workdir / "k6"
        (k6_dir / "options.json").write_text(json.dumps(lease["k6"]["options"]))
        abort = lease.get("abort") or {}
        agg = K6Aggregator(abort.get("error_rate"), int(abort.get("window_s", 30)))
        raw = k6_dir / "raw.json"
        args = [
            "run",
            "--quiet",
            "--no-color",
            "--summary-export",
            "summary.json",
            "--out",
            f"json={raw.name}",
            lease["k6"].get("script", "scenarios.js"),
        ]
        handle = await self.driver.start(lease["lease_id"], self._spec("k6", args, k6_dir, env, secrets, limits))
        reader = _Tail(raw)

        def tick() -> None:
            for line in reader.read_lines():
                agg.feed_line(line)
            state.progress = {**agg.progress(), "generator_cpu": round(state.max_cpu, 3)}
            if agg.abort_reason and not state.abort_reason:
                state.abort_reason = agg.abort_reason

        code = await self._supervise(handle, state, limits.cpu, tick)
        tick()
        state.exit_codes["k6"] = code
        if code == K6_ABORT_EXIT and not state.abort_reason:
            state.abort_reason = "k6 abort threshold crossed (error rate)"
        state.vu_minutes = agg.vu_minutes()
        (k6_dir / "timeseries.json").write_text(json.dumps(agg.timeseries()))
        state.outputs["k6_summary:summary.json"] = k6_dir / "summary.json"
        state.outputs["k6_timeseries:timeseries.json"] = k6_dir / "timeseries.json"
        if raw.exists() and raw.stat().st_size <= self.config.max_raw_upload_bytes * 8:
            gz = k6_dir / "raw.json.gz"
            with raw.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            if gz.stat().st_size <= self.config.max_raw_upload_bytes:
                state.outputs["k6_raw:raw.json.gz"] = gz
        state.outputs["log_bundle:runner.log"] = k6_dir / "k6.log"

    # --------------------------------------------------------- functional ----
    async def _run_functional(
        self,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> None:
        specs = (lease.get("playwright") or {}).get("specs") or []
        if specs:
            pw_dir = workdir / "playwright"
            pw_env = {**env, "PW_REPORT_FILE": "report.json", "PW_OUTPUT_DIR": "test-results", "CI": "1"}
            handle = await self.driver.start(
                lease["lease_id"],
                self._spec(
                    "playwright", ["test", "--config", "playwright.config.ts", *specs], pw_dir, pw_env, secrets, limits
                ),
            )
            state.exit_codes["playwright"] = await self._supervise(handle, state, limits.cpu)
            state.outputs["playwright_report:report.json"] = pw_dir / "report.json"
            results = pw_dir / "test-results"
            if results.exists():
                bundle = workdir / "traces.tar.gz"
                with tarfile.open(bundle, "w:gz") as tar:
                    tar.add(results, arcname="test-results")
                state.outputs["playwright_trace:traces.tar.gz"] = bundle
            state.outputs["log_bundle:runner.log"] = pw_dir / "playwright.log"
        smoke = lease.get("k6_smoke")
        if smoke and not state.cancelled.is_set():
            k6_dir = workdir / "k6"
            (k6_dir / "options.json").write_text(json.dumps(smoke))
            handle = await self.driver.start(
                lease["lease_id"],
                self._spec(
                    "k6",
                    ["run", "--quiet", "--no-color", "--summary-export", "summary.json", "scenarios.js"],
                    k6_dir,
                    env,
                    secrets,
                    limits,
                ),
            )
            state.exit_codes["k6"] = await self._supervise(handle, state, limits.cpu)
            state.outputs["k6_summary:summary.json"] = k6_dir / "summary.json"

    # -------------------------------------------------------------- probes ----
    async def _run_probes(
        self,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> None:
        k6_dir = workdir / "k6"
        results: dict[str, Any] = {}
        for i, probe in enumerate(lease.get("probes") or []):
            if state.cancelled.is_set():
                break
            out = f"probe-{i}.json"
            handle = await self.driver.start(
                lease["lease_id"],
                self._spec(
                    "k6",
                    ["run", "--quiet", "--no-color", "--summary-export", out, probe["path"]],
                    k6_dir,
                    env,
                    secrets,
                    limits,
                ),
            )
            code = await self._supervise(handle, state, limits.cpu)
            summary = {}
            with contextlib.suppress(OSError, ValueError):
                summary = json.loads((k6_dir / out).read_text())
            metrics = summary.get("metrics", {})
            checks = metrics.get("checks", {})
            results[f"{probe['workflow']}/{probe['invariant_id']}"] = {
                "passed": code == 0,
                "exit_code": code,
                "checks_failed": checks.get("fails", 0),
                "successes": (metrics.get("probe_successes") or {}).get("count", 0),
                "failed_thresholds": [
                    f"{m} {t}"
                    for m, d in metrics.items()
                    for t, failed in (d.get("thresholds") or {}).items()
                    if failed
                ],
            }
        (k6_dir / "probes.json").write_text(json.dumps(results))
        state.outputs["k6_summary:probes.json"] = k6_dir / "probes.json"
        state.outputs["log_bundle:runner.log"] = k6_dir / "k6.log"

    # ---------------------------------------------------------- validation ----
    async def _run_validation(
        self,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> None:
        """Dry-run each generated case once (FR-10): k6 at 1 VU / 1 iteration, Playwright once."""
        cases = lease.get("cases") or []
        results: dict[str, Any] = {}
        for case in cases:
            if state.cancelled.is_set():
                break
            outcomes = []
            for attempt in range(int(lease.get("repeats", 1))):
                if case["type"] == "k6_scenario":
                    k6_dir = workdir / "k6"
                    opts = {
                        "scenarios": {
                            "dry": {
                                "executor": "per-vu-iterations",
                                "vus": 1,
                                "iterations": 1,
                                "exec": case["exec_name"],
                                "maxDuration": "60s",
                            }
                        },
                        "thresholds": {"checks": ["rate==1"], "http_req_failed": ["rate==0"]},
                    }
                    (k6_dir / "options.json").write_text(json.dumps(opts))
                    handle = await self.driver.start(
                        lease["lease_id"],
                        self._spec(
                            "k6", ["run", "--quiet", "--no-color", "scenarios.js"], k6_dir, env, secrets, limits
                        ),
                    )
                    code = await self._supervise(handle, state, limits.cpu)
                    log_tail = _tail_text(k6_dir / "k6.log")
                elif case["type"] == "playwright":
                    pw_dir = workdir / "playwright"
                    report = f"report-{attempt}.json"
                    handle = await self.driver.start(
                        lease["lease_id"],
                        self._spec(
                            "playwright",
                            ["test", "--config", "playwright.config.ts", "--retries", "0", case["spec"]],
                            pw_dir,
                            {**env, "PW_REPORT_FILE": report, "CI": "1"},
                            secrets,
                            limits,
                        ),
                    )
                    code = await self._supervise(handle, state, limits.cpu)
                    log_tail = _tail_text(pw_dir / report, 4000) or _tail_text(pw_dir / "playwright.log")
                else:
                    continue
                outcomes.append({"exit_code": code, "log_tail": log_tail if code else None})
            results[case["id"]] = outcomes
        state.result = {"validation": results}

    # ---------------------------------------------------- browser exploration ----
    async def _run_explore(
        self,
        lease: dict[str, Any],
        workdir: Path,
        env: dict[str, str],
        secrets: dict[str, str],
        limits: Limits,
        state: JobState,
    ) -> None:
        """Bounded crawl of the verified target (FR-8). Test credentials arrive sealed and are
        written only into the explorer's config inside the job's working directory."""
        explore = dict(lease.get("explore") or {})
        cfg = {
            **explore,
            "base_url": env.get("BASE_URL") or explore.get("base_url"),
            "credentials": {
                "username": secrets.get(explore.get("username_secret", ""), ""),
                "password": secrets.get(explore.get("password_secret", ""), ""),
            },
        }
        out_dir = workdir / "explore"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "config.json").write_text(json.dumps(cfg))
        handle = await self.driver.start(
            lease["lease_id"],
            self._spec("explorer", ["config.json", "exploration.json"], out_dir, env, {}, limits),
        )
        state.exit_codes["explorer"] = await self._supervise(handle, state, limits.cpu)
        (out_dir / "config.json").unlink(missing_ok=True)
        try:
            result = json.loads((out_dir / "exploration.json").read_text())
        except (OSError, ValueError):
            result = {"error": _tail_text(out_dir / "explorer.log") or "the explorer produced no result"}
        state.result["exploration"] = result

    # ------------------------------------------------------- repair checks ----
    async def _run_checks(self, lease: dict[str, Any], state: JobState) -> None:
        """Allowlisted commands (tests, build, lint) in the sandboxed app, each repeated as asked,
        then the installed security scanners over the source tree (verification gates)."""
        if state.sandbox is None:
            raise SandboxError("checks", "repair checks need a sandbox")
        results: list[dict[str, Any]] = []
        for check in lease.get("checks") or []:
            argv = [str(a) for a in check.get("argv") or []]
            if not allowed_command(argv):
                results.append({"id": check.get("id"), "attempt": 0, "refused": True, "exit_code": None})
                continue
            for attempt in range(max(1, min(int(check.get("repeat", 1)), 5))):
                if state.cancelled.is_set() or state.abort_reason:
                    break
                state.progress = {"check": check.get("id"), "attempt": attempt + 1}
                res = await state.sandbox.exec(argv, float(check.get("timeout_s", 600)))
                results.append(
                    {
                        "id": check.get("id"),
                        "attempt": attempt + 1,
                        "exit_code": res.exit_code,
                        "seconds": res.seconds,
                        "timed_out": res.timed_out,
                        "output": res.output,
                    }
                )
        state.result["checks"] = results
        if lease.get("scan"):
            state.progress = {"check": "security_scan"}
            state.result["scan"] = await scan_tree(state.sandbox.source_dir, self.config.semgrep_config)


class _Tail:
    """Reads complete lines appended to a file since the last call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pos = 0
        self._buf = b""

    def read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            fh.seek(self._pos)
            chunk = fh.read()
            self._pos = fh.tell()
        data = self._buf + chunk
        lines = data.split(b"\n")
        self._buf = lines.pop()
        return [line.decode(errors="replace") for line in lines if line]


def _tail_text(path: Path, limit: int = 2000) -> str | None:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return None


async def _sha_size(path: Path) -> tuple[bytes, str, int]:
    data = await asyncio.to_thread(path.read_bytes)
    return data, hashlib.sha256(data).hexdigest(), len(data)


async def upload_outputs(
    client: LeaseClient, uploads: dict[str, str], outputs: dict[str, Path]
) -> list[dict[str, Any]]:
    reported = []
    for name, path in outputs.items():
        url = uploads.get(name)
        if url is None or not path.exists():
            continue
        data, sha, size = await _sha_size(path)
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        try:
            await client.upload(url, data, content_type)
        except Exception:
            log.warning("upload failed", extra={"artifact": name}, exc_info=True)
            continue
        reported.append({"name": name, "sha256": sha, "size": size, "content_type": content_type})
    return reported
