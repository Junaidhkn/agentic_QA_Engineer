"""Compose projects for Docker sandboxes (ADR-0009, ADR-0011).

``rewrite_compose`` hardens a customer compose file before it runs: every service runs under
gVisor (``runsc``) on the job's internal network with no published ports, no privileged mode,
host namespaces, devices, capabilities or host bind mounts (relative paths inside the source
tree are allowed), and with CPU/memory/PID limits. ``generate_compose`` builds the same kind of
project for Dockerfile and template strategies. Both add the platform sidecars: Postgres/Redis
when needed, the stub server and an OpenTelemetry collector.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = (
    "privileged",
    "cap_add",
    "devices",
    "device_cgroup_rules",
    "pid",
    "ipc",
    "userns_mode",
    "cgroup",
    "cgroup_parent",
    "sysctls",
    "security_opt",
    "extra_hosts",
    "dns",
    "network_mode",
    "ports",
    "profiles",
    "container_name",
    "restart",
    "deploy",
    "links",
    "external_links",
    "volumes_from",
)
STUB_PORT = 8089
COLLECTOR_PORT = 4318
PG_PASSWORD_ENV = "RE_SANDBOX_PG_PASSWORD"  # noqa: S105 - name of an env var, not a password


class ComposeError(ValueError):
    pass


def network_name(job_id: str) -> str:
    return f"re-run-{job_id[:12]}"


def project_name(job_id: str) -> str:
    return f"re-sbx-{job_id[:12]}"


def _limits(cpus: float, mem_gb: float, pids: int) -> dict[str, Any]:
    return {"cpus": str(cpus), "mem_limit": f"{mem_gb}g", "pids_limit": pids}


def _volumes(volumes: list[Any], src_root: Path) -> list[Any]:
    out = []
    for v in volumes or []:
        spec = v if isinstance(v, str) else f"{v.get('source', '')}:{v.get('target', '')}"
        source = spec.split(":", 1)[0]
        if source.startswith(("/", "~")) or (source.startswith(".") and not _inside(src_root, source)):
            raise ComposeError(f"host bind mount '{source}' is not allowed in sandboxes")
        out.append(v)
    return out


def _inside(root: Path, rel: str) -> bool:
    p = (root / rel).resolve()
    return p == root.resolve() or root.resolve() in p.parents


def _env_dict(env: Any) -> dict[str, str]:
    if isinstance(env, list):
        return dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
    return {str(k): "" if v is None else str(v) for k, v in (env or {}).items()}


def harden(
    service: dict[str, Any], job_id: str, runtime: str, limits: dict[str, Any], src_root: Path
) -> dict[str, Any]:
    svc = {k: v for k, v in service.items() if k not in FORBIDDEN_KEYS}
    if service.get("ports"):
        svc["expose"] = sorted(
            {str(p).split(":")[-1].split("/")[0] for p in service["ports"] if not isinstance(p, dict)}
            | {str(p["target"]) for p in service["ports"] if isinstance(p, dict) and p.get("target")}
        )
    if "build" in svc:
        ctx = svc["build"] if isinstance(svc["build"], str) else svc["build"].get("context", ".")
        if not _inside(src_root, str(ctx)):
            raise ComposeError(f"build context '{ctx}' is outside the source tree")
        if isinstance(svc["build"], dict):
            svc["build"] = {k: v for k, v in svc["build"].items() if k in ("context", "dockerfile", "args", "target")}
    svc["volumes"] = _volumes(svc.get("volumes") or [], src_root)
    if not svc["volumes"]:
        svc.pop("volumes")
    svc["runtime"] = runtime
    svc["networks"] = ["run"]
    svc["security_opt"] = ["no-new-privileges:true"]
    svc.update(limits)
    return svc


def _sidecars(plan: dict[str, Any], runtime: str, images: dict[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    services = plan.get("services") or {}
    pg = services.get("postgres")
    if pg and not pg.get("from_compose"):
        out["postgres"] = {
            "image": pg.get("image") or images["postgres"],
            "environment": {
                "POSTGRES_USER": pg.get("user", "sandbox"),
                "POSTGRES_DB": pg.get("database", "app"),
                "POSTGRES_PASSWORD": f"${{{PG_PASSWORD_ENV}}}",
            },
            "command": [
                "postgres",
                "-c",
                "shared_preload_libraries=pg_stat_statements",
                "-c",
                "pg_stat_statements.track=all",
                "-c",
                "max_connections=100",
            ],
            "tmpfs": ["/var/lib/postgresql/data:size=1g"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"], "interval": "2s", "retries": 60},
            **_limits(1, 1, 512),
        }
    if services.get("redis") and not services["redis"].get("from_compose"):
        out["redis"] = {
            "image": services["redis"].get("image") or images["redis"],
            "command": ["redis-server", "--save", "", "--appendonly", "no"],
            **_limits(0.5, 0.5, 256),
        }
    if plan.get("stubs"):
        out["stub"] = {
            "image": images["stub"],
            "command": [
                "python",
                "-m",
                "runner_agent.sandbox_env.stub",
                "--config",
                "/re/stubs.json",
                "--port",
                str(STUB_PORT),
            ],
            "volumes": ["./.re-sandbox/stubs.json:/re/stubs.json:ro"],
            **_limits(0.25, 0.25, 128),
        }
    out["otel-collector"] = {
        "image": images["collector"],
        "command": ["--config=/re/collector.yaml"],
        "volumes": ["./.re-sandbox/collector.yaml:/re/collector.yaml:ro"],
        **_limits(0.5, 0.5, 256),
    }
    for svc in out.values():
        svc.update({"runtime": runtime, "networks": ["run"], "security_opt": ["no-new-privileges:true"]})
    if "postgres" in out:  # the official image needs to chown its data directory before dropping root
        out["postgres"]["cap_drop"] = ["ALL"]
        out["postgres"]["cap_add_allowlisted"] = ["CHOWN", "SETUID", "SETGID", "FOWNER", "DAC_OVERRIDE"]
    for name, svc in out.items():
        if name != "postgres":
            svc["cap_drop"] = ["ALL"]
            svc["read_only"] = True
    return out


def app_environment(plan: dict[str, Any], run_id: str | None) -> dict[str, str]:
    port = int(plan.get("port") or 8000)
    env = {k: v.replace("{port}", str(port)) for k, v in (plan.get("env") or {}).items()}
    env["PORT"] = str(port)
    for name in plan.get("placeholders", []):
        env[name] = "sandbox-placeholder"
    pg = (plan.get("services") or {}).get("postgres")
    if pg:
        host = pg.get("from_compose") or "postgres"
        url = (
            f"{plan.get('database_scheme') or 'postgresql'}://{pg.get('user', 'sandbox')}:${{{PG_PASSWORD_ENV}}}"
            f"@{host}:5432/{pg.get('database', 'app')}"
        )
        for name in plan.get("database_env") or ["DATABASE_URL"]:
            env[name] = url
    redis = (plan.get("services") or {}).get("redis")
    if redis:
        env["REDIS_URL"] = f"redis://{redis.get('from_compose') or 'redis'}:6379/0"
    for stub in plan.get("stubs", []):
        env[stub["env"]] = f"http://stub:{STUB_PORT}/{stub['name']}"
    env.update(
        {
            "OTEL_SERVICE_NAME": plan.get("app_service") or "app",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://otel-collector:{COLLECTOR_PORT}",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment=sandbox,re.run_id={run_id or ''}",
        }
    )
    return env


def _finalize(services: dict[str, Any], job_id: str) -> dict[str, Any]:
    for svc in services.values():
        caps = svc.pop("cap_add_allowlisted", None)
        if caps:
            svc["cap_add"] = caps
    return {
        "name": project_name(job_id),
        "services": services,
        "networks": {"run": {"external": True, "name": network_name(job_id)}},
    }


def rewrite_compose(
    doc: dict[str, Any],
    plan: dict[str, Any],
    job_id: str,
    runtime: str,
    images: dict[str, str],
    src_root: Path,
    run_id: str | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    services = doc.get("services") or {}
    if not services:
        raise ComposeError("compose file has no services")
    app = plan.get("app_service") or next(iter(services))
    out: dict[str, Any] = {}
    for name, svc in services.items():
        hardened = harden(svc or {}, job_id, runtime, limits or _limits(2, 4, 2048), src_root)
        if name == app:
            env = _env_dict(hardened.get("environment"))
            env.update(app_environment(plan, run_id))
            hardened["environment"] = env
        out[name] = hardened
    for name, sidecar in _sidecars(plan, runtime, images).items():
        out.setdefault(name, sidecar)
    return _finalize(out, job_id)


def generate_compose(
    plan: dict[str, Any],
    job_id: str,
    runtime: str,
    images: dict[str, str],
    app_image: str,
    run_id: str | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    app: dict[str, Any] = {
        "image": app_image,
        "environment": app_environment(plan, run_id),
        "expose": [str(plan.get("port") or 8000)],
        "runtime": runtime,
        "networks": ["run"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:size=256m"],
        **(limits or _limits(2, 4, 2048)),
    }
    services = {"app": app, **_sidecars(plan, runtime, images)}
    deps = [n for n in ("postgres", "redis", "stub") if n in services]
    if deps:
        app["depends_on"] = {
            n: {"condition": "service_healthy" if n == "postgres" else "service_started"} for n in deps
        }
    return _finalize(services, job_id)


def render_dockerfile(plan: dict[str, Any]) -> str:
    template = resources.files("runner_agent.sandbox_env").joinpath(f"templates/{plan['template']}.Dockerfile")
    text = template.read_text()

    def runs(cmds: list[list[str]]) -> str:
        return "\n".join(f"RUN {json.dumps(c)}" for c in cmds)

    port = str(plan.get("port") or 8000)
    start = [a.replace("{port}", port) for a in (plan.get("start") or [])]
    return (
        text.replace("{{INSTALL}}", runs(plan.get("install", [])))
        .replace("{{BUILD}}", runs(plan.get("build", [])))
        .replace("{{START}}", f"CMD {json.dumps(start)}")
    )


def collector_config(otlp_endpoint: str, token: str, proxy: str | None) -> str:
    """Collector inside the sandbox: receives OTLP from the app, forwards to the platform's OTLP
    gateway with the run-scoped token (the only egress host it is allowed)."""
    lines = [
        "receivers:",
        "  otlp:",
        "    protocols:",
        f"      http: {{endpoint: 0.0.0.0:{COLLECTOR_PORT}}}",
        "processors:",
        "  batch: {}",
        "exporters:",
        "  otlphttp:",
        f"    endpoint: {json.dumps(otlp_endpoint)}",
        "    headers:",
        f"      authorization: {json.dumps('Bearer ' + token)}",
        "service:",
        "  pipelines:",
        "    traces: {receivers: [otlp], processors: [batch], exporters: [otlphttp]}",
        "    metrics: {receivers: [otlp], processors: [batch], exporters: [otlphttp]}",
        "    logs: {receivers: [otlp], processors: [batch], exporters: [otlphttp]}",
    ]
    return "\n".join(lines) + "\n"
