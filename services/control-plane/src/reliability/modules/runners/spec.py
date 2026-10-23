"""Build the lease handed to a runner: everything one job needs and nothing more
(ADR-0008): scoped presigned URLs, a run-scoped OTLP token, the egress allowlist and
secrets sealed to the runner's key."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from reliability.modules.artifacts import service as artifacts
from reliability.modules.runs.models import Run
from reliability.modules.secrets import service as secrets
from reliability.modules.targets.models import Target
from reliability.modules.testsuites import service as suites
from reliability.platform.actor import Actor
from reliability.platform.crypto import seal_for_x25519, sign_token
from reliability.platform.settings import get_settings
from reliability.platform.storage import get_store

UPLOADS = {
    "load": ["k6_summary:summary.json", "k6_raw:raw.json.gz", "k6_timeseries:timeseries.json", "log_bundle:runner.log"],
    "functional": [
        "playwright_report:report.json",
        "playwright_trace:traces.tar.gz",
        "k6_summary:summary.json",
        "log_bundle:runner.log",
    ],
    "probe": ["k6_summary:probes.json", "log_bundle:runner.log"],
    "verification": [
        "k6_summary:summary.json",
        "k6_raw:raw.json.gz",
        "k6_timeseries:timeseries.json",
        "log_bundle:runner.log",
    ],
}
LIMITS = {
    "load": {"cpu": 4, "mem_gb": 8, "pids": 4096},
    "functional": {"cpu": 2, "mem_gb": 4, "pids": 2048},
    "probe": {"cpu": 2, "mem_gb": 2, "pids": 1024},
    "verification": {"cpu": 4, "mem_gb": 8, "pids": 4096},
}
CONTENT_TYPES = {".json": "application/json", ".gz": "application/gzip", ".log": "text/plain"}


def upload_key(run: Run, name: str) -> str:
    kind, filename = name.split(":", 1)
    return artifacts.new_key(run.org_id, run.project_id, kind, run.id, filename)


async def build_run_spec(
    session: AsyncSession, run: Run, target: Target | None, lease_id: str, runner_id: str, runner_public_key: str
) -> dict[str, Any]:
    cfg = run.config_snapshot
    store = get_store()
    suite = await suites.get(session, run.suite_id) if run.suite_id else None
    bundles = {}
    if suite is not None:
        for part in suites.BUNDLE_PARTS:
            bundles[part] = artifacts.download_url(await suites.bundle_artifact(session, suite, part), ttl=1800)
    uploads = {
        name: store.presign_put(
            upload_key(run, name),
            ttl=cfg.get("max_wall_s", 1800) + 1800,
            content_type=CONTENT_TYPES.get("." + name.rsplit(".", 1)[-1], "application/octet-stream"),
        )
        for name in UPLOADS[run.kind]
    }
    secret_values = await secrets.resolve_for_run(
        session, Actor("runner", runner_id), run.org_id, run.project_id, set(cfg.get("secret_names") or []), run.id
    )
    think = cfg.get("think_time_s") or [1, 3]
    env = {"THINK_MIN_S": str(think[0]), "THINK_MAX_S": str(think[1])}
    if target is not None:
        env["BASE_URL"] = target.base_url
    egress = {
        "allow_hosts": [target.host] if target else [],
        "allow_ips": [str(ip) for ip in target.resolved_ips or []] if target else [],
    }
    ttl = int(cfg.get("max_wall_s", 1800)) + 1800
    return {
        "lease_id": lease_id,
        "job_type": run.kind,
        "run_id": str(run.id),
        "org_id": str(run.org_id),
        "project_id": str(run.project_id),
        "target": cfg.get("target"),
        "env": env,
        "secrets_sealed": seal_for_x25519(runner_public_key, json.dumps(secret_values).encode()),
        "egress": egress,
        "bundles": bundles,
        "k6": cfg.get("k6"),
        "k6_smoke": cfg.get("k6_smoke"),
        "playwright": cfg.get("playwright"),
        "probes": cfg.get("probes"),
        "abort": cfg.get("abort"),
        "max_wall_s": cfg.get("max_wall_s", 1800),
        "limits": LIMITS[run.kind],
        "uploads": uploads,
        "otlp": {
            "endpoint": get_settings().otlp_public_url,
            "token": sign_token(
                {"org": str(run.org_id), "project": str(run.project_id), "run": str(run.id)}, "otlp", ttl
            ),
        },
        "heartbeat_interval_s": 3,
    }
