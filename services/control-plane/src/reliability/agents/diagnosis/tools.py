"""Read-only investigation tools for the diagnosis agent (agent-workflows §5).

Each tool reads data that belongs to this finding's run — the run summary and time series,
sandbox database snapshots, the code snapshot at the run's commit, and (when configured) the
tenant's traces and logs through ``TelemetryQuery`` with the tenant fixed by the platform.
Every result is stored as an evidence row and returned with its ``evidence_id`` so it can be
cited. None of these tools can write, run code, reach a live customer database or change any
setting (ADR-0007).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from reliability.agents.common.budget import GraphBudget
from reliability.agents.common.tools import Tool, ToolError, ToolRegistry
from reliability.agents.common.untrusted import wrap
from reliability.agents.diagnosis.code import CodeIndex
from reliability.agents.diagnosis.evidence import EvidenceSink

Telemetry = Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class ReadCode(BaseModel):
    path: str = Field(description="file path in the repository")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SearchCode(BaseModel):
    pattern: str = Field(description="regular expression", max_length=200)


class MetricWindow(BaseModel):
    scope: str = Field(description='"overall" or "endpoint:METHOD /path" as listed in the evidence pack')


class DbStats(BaseModel):
    kind: str = Field(description="tables | connections | statements")


class HttpSamples(BaseModel):
    kind: str = Field(description="errors | slowest")
    endpoint: str | None = None


class FindTraces(BaseModel):
    endpoint: str | None = None
    min_duration_ms: int | None = Field(default=None, ge=0)
    errors_only: bool = False


class SearchLogs(BaseModel):
    text: str = Field(max_length=200)


def build_tools(
    budget: GraphBudget,
    sink: EvidenceSink,
    run: dict[str, Any],
    code: CodeIndex | None,
    telemetry: Telemetry | None,
    audit: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> ToolRegistry:
    allowed: set[Any] = {"read_tenant_data", "read_telemetry"} | ({"read_code"} if code else set())
    registry = ToolRegistry("diagnosis", allowed, budget, audit)

    async def emit(kind: str, locator: dict[str, Any], summary: str, data: Any) -> str:
        ev = await sink.add_evidence(kind, locator, summary, data if isinstance(data, dict) else {"items": data})
        body = json.dumps(data, default=str)[:12000]
        return f"evidence_id: {ev.id}\nsummary: {summary}\n" + wrap(kind, json.dumps(locator, default=str), body)

    if code is not None:

        async def read_code(a: ReadCode) -> str:
            if not code.exists(a.path):
                raise ToolError(f"no such file {a.path}")
            end = min(a.end_line, a.start_line + 199, code.line_count(a.path))
            text = code.read(a.path, a.start_line, end)
            return await emit(
                "code_location",
                {"path": a.path, "lines": [a.start_line, end], "commit": code.commit, "signal": "read"},
                f"{a.path}:{a.start_line}-{end}",
                {"excerpt": text},
            )

        async def search_code(a: SearchCode) -> str:
            hits = code.search(a.pattern)
            return await emit(
                "code_location",
                {
                    "search": a.pattern,
                    "commit": code.commit,
                    "signal": "search",
                    "path": hits[0]["path"] if hits else None,
                    "lines": [hits[0]["line"], hits[0]["line"]] if hits else None,
                },
                f"{len(hits)} matches for /{a.pattern}/",
                hits,
            )

        registry.register(
            Tool("read_code", "Read lines of a source file at the run's commit.", ReadCode, "read_code", read_code)
        )
        registry.register(
            Tool(
                "search_code",
                "Search the source at the run's commit with a regex.",
                SearchCode,
                "read_code",
                search_code,
            )
        )

    async def metric_window(a: MetricWindow) -> str:
        rows = {"overall": run.get("overall") or {}, **(run.get("endpoints") or {})}
        row = rows.get(a.scope)
        if row is None:
            raise ToolError(f"unknown scope; available: {sorted(rows)[:20]}")
        series = (run.get("series") or {}).get(a.scope, [])[:120]
        return await emit(
            "metric_window",
            {"run_id": run["id"], "scope": a.scope, "source": "run_timeseries"},
            f"{a.scope}: p95 {row.get('p95_ms')} ms, errors {row.get('error_rate')}, {row.get('requests')} requests",
            {**row, "series": series},
        )

    async def db_stats(a: DbStats) -> str:
        db = (run.get("sandbox_report") or {}).get("db") or {}
        if not db:
            raise ToolError("no database snapshot: the run was not executed in a sandbox")
        data = {
            "tables": db.get("tables"),
            "connections": {
                "end": db.get("connections"),
                "samples": (run.get("sandbox_report") or {}).get("db_samples", [])[-60:],
            },
            "statements": db.get("statements"),
        }.get(a.kind)
        if data is None:
            raise ToolError("kind must be tables, connections or statements")
        return await emit(
            "db_stat",
            {"run_id": run["id"], "source": "sandbox", "stat": a.kind},
            f"sandbox database {a.kind} snapshot",
            data,
        )

    async def http_samples(a: HttpSamples) -> str:
        pool = (run.get("samples") or {}).get("errors" if a.kind == "errors" else "slowest", [])
        picked = [s for s in pool if not a.endpoint or s.get("endpoint") == a.endpoint][:10]
        return await emit(
            "http_sample",
            {"run_id": run["id"], "kind": a.kind, "endpoint": a.endpoint},
            f"{len(picked)} {a.kind} request samples",
            picked,
        )

    registry.register(
        Tool(
            "metric_window",
            "Latency/error/throughput for a scope of this run, with series.",
            MetricWindow,
            "read_tenant_data",
            metric_window,
        )
    )
    registry.register(
        Tool("db_stats", "Sandbox database statistics captured during the run.", DbStats, "read_tenant_data", db_stats)
    )
    registry.register(
        Tool(
            "http_samples",
            "Failed or slowest request samples recorded by the load generator.",
            HttpSamples,
            "read_tenant_data",
            http_samples,
        )
    )

    if telemetry is not None:

        async def find_traces(a: FindTraces) -> str:
            traces = await telemetry("traces", a.model_dump())
            return await emit(
                "trace", {"run_id": run["id"], "query": a.model_dump()}, f"{len(traces)} traces", traces[:20]
            )

        async def search_logs(a: SearchLogs) -> str:
            lines = await telemetry("logs", a.model_dump())
            return await emit(
                "log_excerpt", {"run_id": run["id"], "text": a.text}, f"{len(lines)} log lines", lines[:200]
            )

        registry.register(
            Tool("find_traces", "Find traces of this run (span breakdown).", FindTraces, "read_telemetry", find_traces)
        )
        registry.register(
            Tool(
                "search_logs",
                "Search this run's application logs (redacted).",
                SearchLogs,
                "read_telemetry",
                search_logs,
            )
        )
    return registry
