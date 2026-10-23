"""Runs the discovery graph in an ``agents`` worker (ADR-0006). The ports below give the graph
tenant-scoped access to exactly what it needs; the graph never touches the database itself."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from reliability.agents.common.budget import GraphBudget
from reliability.agents.discovery import graph
from reliability.agents.discovery.snapshot import CodeSnapshot, from_archive
from reliability.llm import get_gateway
from reliability.modules.artifacts import service as artifacts
from reliability.modules.artifacts.models import Artifact
from reliability.modules.discovery.models import DiscoveryRun, OpenAPISpec
from reliability.modules.discovery.service import ARCHIVE_SHA
from reliability.modules.integrations.github import service as github
from reliability.modules.integrations.github.models import Repository
from reliability.modules.quotas.llm_sink import LedgerUsageSink
from reliability.modules.secrets import service as secrets
from reliability.modules.tenancy.models import Organization
from reliability.modules.workflows import service as workflows
from reliability.platform.db import tenant_tx
from reliability.platform.errors import AppError
from reliability.platform.flags import is_enabled
from reliability.platform.jobs import job

log = logging.getLogger(__name__)
LLM_DISABLED_FLAG = "agents.discovery.llm_disabled"


class Cancelled(Exception):
    pass


class RunPorts:
    def __init__(self, run: DiscoveryRun) -> None:
        self.run_id, self.org_id, self.project_id = run.id, run.org_id, run.project_id
        self.options = dict(run.options)

    def _tx(self) -> Any:
        return tenant_tx(org_ids=[self.org_id])

    async def snapshot(self) -> tuple[str, CodeSnapshot]:
        async with self._tx() as s:
            if archive := self.options.get("archive_artifact_id"):
                art = await s.get(Artifact, uuid.UUID(archive))
                if art is None:
                    raise AppError("NOT_FOUND", "source archive expired; upload it again")
                return ARCHIVE_SHA, from_archive(artifacts.read(art))
            if repo_id := self.options.get("repository_id"):
                repo = await s.get(Repository, uuid.UUID(repo_id))
                if repo is None:
                    raise AppError("CONFLICT", "repository is no longer connected")
                sha, data = await github.snapshot(s, repo, self.options.get("commit_ref") or repo.default_branch)
                run = await s.get(DiscoveryRun, self.run_id)
                if run is not None:
                    run.commit_sha = sha
                return sha, from_archive(data)
        return ARCHIVE_SHA, CodeSnapshot()

    async def openapi_documents(self) -> list[str]:
        docs = []
        async with self._tx() as s:
            for spec_id in self.options.get("openapi_spec_ids", []):
                spec = await s.get(OpenAPISpec, uuid.UUID(spec_id))
                if spec is None:
                    continue
                art = (
                    await s.execute(select(Artifact).where(Artifact.storage_key == spec.artifact_key))
                ).scalar_one_or_none()
                if art is not None:
                    docs.append(artifacts.read(art).decode("utf-8", errors="replace"))
        return docs

    async def secret_names(self) -> set[str]:
        async with self._tx() as s:
            return {x.name for x in await secrets.list_names(s, self.project_id)}

    async def progress(self, step: str, detail: dict[str, Any]) -> None:
        async with self._tx() as s:
            run = await s.get(DiscoveryRun, self.run_id, with_for_update=True)
            if run is None or run.status == "cancelled":
                raise Cancelled()
            run.progress = [*run.progress, {"step": step, "at": datetime.now(UTC).isoformat(), **detail}]

    async def persist(self, candidates: list[dict[str, Any]], stack: dict[str, Any]) -> dict[str, int]:
        counts = {"created": 0, "updated": 0, "unchanged": 0, "rejected": 0}
        async with self._tx() as s:
            for c in candidates:
                try:
                    before = await workflows.find_by_key(s, self.project_id, c["ir"]["key"])
                    wf = await workflows.upsert_candidate(
                        s,
                        org_id=self.org_id,
                        project_id=self.project_id,
                        ir=c["ir"],
                        source=c["source"],
                        confidence=float(c["confidence"]),
                        discovery_run_id=self.run_id,
                    )
                except AppError:
                    counts["rejected"] += 1
                    continue
                counts["created" if before is None else "updated" if wf is not None else "unchanged"] += 1
            if repo_id := self.options.get("repository_id"):
                repo = await s.get(Repository, uuid.UUID(repo_id))
                if repo is not None:
                    repo.detected_stack = stack
        return counts


async def _finish(
    run_id: uuid.UUID, org_id: uuid.UUID, status: str, stats: dict[str, Any] | None, error: dict[str, Any] | None
) -> None:
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(DiscoveryRun, run_id, with_for_update=True)
        if run is None:
            return
        if run.status != "cancelled":
            run.status = status
        run.stats, run.error, run.finished_at = stats, error, datetime.now(UTC)


@job("discovery.run", queue="agents", max_retries=2)
async def run_discovery(payload: dict[str, Any]) -> None:
    run_id, org_id = uuid.UUID(payload["discovery_id"]), uuid.UUID(payload["org_id"])
    async with tenant_tx(org_ids=[org_id]) as s:
        run = await s.get(DiscoveryRun, run_id, with_for_update=True)
        if run is None or run.status not in ("queued", "running"):
            return
        org = await s.get(Organization, org_id)
        llm_enabled = bool(org and org.llm_code_context_enabled) and not await is_enabled(s, LLM_DISABLED_FLAG, org_id)
        run.status = "running"
        ports = RunPorts(run)
        thread_id = run.agent_thread_id or f"discovery:{run_id}"
    budget = GraphBudget.for_graph("discovery")
    deps = graph.DiscoveryDeps(ports, get_gateway(), LedgerUsageSink(), budget, org_id, run_id, llm_enabled)
    try:
        result = await graph.run(deps, thread_id)
    except Cancelled:
        await _finish(run_id, org_id, "cancelled", None, None)
        return
    except AppError as exc:
        await _finish(run_id, org_id, "failed", None, {"code": exc.code, "message": exc.message})
        return
    except Exception as exc:
        log.exception("discovery failed", extra={"discovery_id": str(run_id)})
        await _finish(run_id, org_id, "failed", None, {"code": "INTERNAL", "message": type(exc).__name__})
        return
    routes = result.get("routes", [])
    stats = {
        "commit_sha": result.get("commit_sha"),
        "frameworks": result.get("stack", {}).get("frameworks", []),
        "routes": {"api": sum(r["kind"] == "api" for r in routes), "pages": sum(r["kind"] == "page" for r in routes)},
        "operations": len(result.get("operations", [])),
        "openapi_errors": result.get("openapi_errors", []),
        "excluded_files": result.get("excluded_files", 0),
        "candidates": len(result.get("candidates", [])),
        "from_model": sum(1 for c in result.get("candidates", []) if c.get("origin") == "model"),
        "persisted": result.get("persisted", {}),
        "llm": {**result.get("llm", {}), "enabled": llm_enabled, "tokens": budget.spent},
    }
    persisted = result.get("persisted", {})
    status = "awaiting_review" if persisted.get("created") or persisted.get("updated") else "completed"
    await _finish(run_id, org_id, "budget_exhausted" if result.get("budget_exhausted") else status, stats, None)
