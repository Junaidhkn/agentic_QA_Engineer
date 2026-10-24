"""Standard evidence pack (agent-workflows §5, ``collect_context``): deterministic collectors per
finding type. Each collected fact becomes an ``evidence`` row (via the ports) so hypotheses can
cite it and the citation checker can re-read it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from reliability.agents.diagnosis.citations import Evidence
from reliability.agents.diagnosis.code import CodeIndex, Handler


class EvidenceSink(Protocol):
    async def add_evidence(
        self, kind: str, locator: dict[str, Any], summary: str, snapshot: dict[str, Any] | None
    ) -> Evidence: ...


@dataclass
class Pack:
    finding: dict[str, Any]
    endpoint: str | None
    items: list[Evidence] = field(default_factory=list)
    handler: Handler | None = None
    facts: dict[str, Any] = field(default_factory=dict)  # structured facts for the rules (ids → numbers)

    def by_kind(self, *kinds: str) -> list[Evidence]:
        return [e for e in self.items if e.kind in kinds]

    def first(self, kind: str, **locator: Any) -> Evidence | None:
        return next(
            (e for e in self.items if e.kind == kind and all(e.locator.get(k) == v for k, v in locator.items())), None
        )


def finding_endpoint(finding: dict[str, Any]) -> str | None:
    scope = str((finding.get("headline") or {}).get("scope") or "")
    if scope.startswith("endpoint:"):
        return scope.split(":", 1)[1]
    return finding.get("endpoint")


async def collect(
    sink: EvidenceSink,
    finding: dict[str, Any],
    existing: list[Evidence],
    run: dict[str, Any],
    code: CodeIndex | None,
) -> Pack:
    pack = Pack(finding=finding, endpoint=finding_endpoint(finding), items=list(existing))
    requests = int(((run.get("overall") or {}).get("requests")) or 0)
    endpoint_rows: dict[str, dict[str, Any]] = run.get("endpoints") or {}

    # Metric windows for every endpoint of the run (lets hypotheses compare endpoints).
    for scope, row in sorted(endpoint_rows.items()):
        if pack.first("metric_window", scope=scope):
            continue
        name = scope.split(":", 1)[1]
        pack.items.append(
            await sink.add_evidence(
                "metric_window",
                {"run_id": run["id"], "scope": scope, "source": "run_summary"},
                f"{name}: p95 {row.get('p95_ms') or 0:.0f} ms, errors {float(row.get('error_rate') or 0):.2%}, "
                f"{int(row.get('requests') or 0)} requests",
                row,
            )
        )

    # Database statistics captured by the sandbox (never a live customer database).
    report = run.get("sandbox_report") or {}
    db = report.get("db") or {}
    tables = db.get("tables") or []
    if tables:
        enriched = []
        for t in tables:
            accesses = int(t.get("seq_scan") or 0) + int(t.get("idx_scan") or 0)
            rows_per_seq = round(int(t.get("seq_tup_read") or 0) / int(t["seq_scan"]), 1) if t.get("seq_scan") else 0
            enriched.append(
                {
                    **t,
                    "accesses": accesses,
                    "rows_read_per_seq_scan": rows_per_seq,
                    "accesses_per_request": round(accesses / requests, 2) if requests else None,
                }
            )
        pack.facts["tables"] = {t["relname"]: t for t in enriched}
        for t in enriched[:8]:
            ev = await sink.add_evidence(
                "db_stat",
                {"run_id": run["id"], "source": "sandbox", "stat": "table", "table": t["relname"]},
                f"table {t['relname']}: {t.get('seq_scan', 0)} sequential scans reading "
                f"{t.get('seq_tup_read', 0)} rows "
                f"({t['rows_read_per_seq_scan']} per scan), {t.get('idx_scan') or 0} index scans, "
                f"{t.get('n_live_tup', 0)} live rows, {t['accesses_per_request'] or 0} accesses per request "
                f"over {requests} requests",
                {**t, "requests": requests},
            )
            pack.items.append(ev)
    samples = report.get("db_samples") or []
    conns = db.get("connections") or {}
    if samples or conns:
        busy = [int(s.get("active") or 0) + int(s.get("idle_in_transaction") or 0) for s in samples]
        peak = max(busy) if busy else 0
        plateau = sum(1 for b in busy if b == peak) / len(busy) if busy else 0
        snapshot = {
            "peak_busy": peak,
            "plateau_fraction": round(plateau, 2),
            "samples": samples[-60:],
            "max_connections": conns.get("max"),
            "end": conns,
        }
        pack.facts["connections"] = snapshot
        pack.items.append(
            await sink.add_evidence(
                "db_stat",
                {"run_id": run["id"], "source": "sandbox", "stat": "connections"},
                f"database connections busy (active + idle in transaction): peak {peak} in {len(samples)} samples, "
                f"at the peak {plateau:.0%} of the time; server max_connections {conns.get('max', 'n/a')}",
                snapshot,
            )
        )
    for q in (db.get("statements") or [])[:5]:
        pack.items.append(
            await sink.add_evidence(
                "db_stat",
                {"run_id": run["id"], "source": "sandbox", "stat": "statement"},
                f"query called {q.get('calls')} times, mean {q.get('mean_ms')} ms, total {q.get('total_ms')} ms: "
                f"{str(q.get('query'))[:160]}",
                q,
            )
        )

    # Code on the hot path (snapshot at the run's commit).
    if code is not None and pack.endpoint:
        h = code.handler(pack.endpoint)
        pack.handler = h
        if h is not None:
            pack.items.append(
                await _code(
                    sink,
                    code,
                    h.path,
                    [h.start, h.end],
                    f"handler for {h.endpoint} ({h.path}:{h.start}-{h.end})",
                    "handler",
                )
            )
            for line in code.db_calls_in_loops(h):
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        h.path,
                        [line, line],
                        f"{h.path}:{line} runs a database query inside a loop in the {h.endpoint} handler",
                        "db_call_in_loop",
                    )
                )
            for line in code.blocking_calls(h):
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        h.path,
                        [line, line],
                        f"{h.path}:{line} makes a blocking call (sleep/sync I/O) in the {h.endpoint} handler",
                        "blocking_call",
                    )
                )
            for line in code.generic_raises(h):
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        h.path,
                        [line, line],
                        f"{h.path}:{line} raises an unhandled generic exception in the "
                        f"{h.endpoint} handler (becomes HTTP 500)",
                        "generic_raise",
                    )
                )
            runtime = _runtime_indexes(db)
            seen: set[tuple[str, str]] = set()
            for table, col, line in code.filter_columns(h):
                if (table, col) in seen:
                    continue
                seen.add((table, col))
                declared = code.has_index(table, col)
                if runtime is not None:  # the sandbox database is the source of truth
                    if table not in runtime_tables(db) or any(
                        f"({col})" in d or f" {col}," in d or f"({col}," in d for d in runtime.get(table, [])
                    ):
                        continue
                elif declared or table not in code.model_tables().values():
                    continue
                where = "declared in the code but missing from the sandbox database" if declared else "not declared"
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        h.path,
                        [line, line],
                        f"{h.path}:{line} filters {table} by {col}; no index on {table}.{col} "
                        + (
                            "exists in the sandbox database"
                            if runtime is not None
                            else "is declared in the models or migrations"
                        )
                        + f" ({where})",
                        "unindexed_filter",
                        {"table": table, "column": col, "declared_in_code": declared},
                    )
                )
            racy = code.unlocked_read_modify_write(h)
            for line in racy[:2]:
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        h.path,
                        [line, line],
                        f"{h.path}:{line} writes a value read earlier in {h.endpoint} without a lock or atomic update",
                        "unlocked_write",
                    )
                )
        for setting in code.pool_settings():
            if setting["value"] <= 5:
                pack.items.append(
                    await _code(
                        sink,
                        code,
                        setting["path"],
                        [setting["line"], setting["line"]],
                        f"{setting['path']}:{setting['line']} sets {setting['setting']}={setting['value']} for the "
                        f"database connection pool",
                        "pool_setting",
                        {"value": setting["value"]},
                    )
                )
    errors = [
        s
        for s in (run.get("samples") or {}).get("errors", [])
        if not pack.endpoint or s.get("endpoint") == pack.endpoint
    ][:5]
    for s in errors:
        if not any(e.kind == "http_sample" and e.locator.get("trace_id") == s.get("trace_id") for e in pack.items):
            pack.items.append(
                await sink.add_evidence(
                    "http_sample",
                    {"run_id": run["id"], "endpoint": s.get("endpoint"), "trace_id": s.get("trace_id")},
                    f"{s.get('endpoint')} failed with status {s.get('status')} after {s.get('duration_ms')} ms"
                    f"{': ' + str(s.get('error'))[:200] if s.get('error') else ''}",
                    s,
                )
            )
    return pack


def _runtime_indexes(db: dict[str, Any]) -> dict[str, list[str]] | None:
    """table → index definitions, when the sandbox snapshot includes them."""
    indexes = db.get("indexes")
    if not db.get("tables") or indexes is None or any("indexdef" not in i for i in indexes):
        return None
    out: dict[str, list[str]] = {}
    for i in indexes:
        out.setdefault(i["relname"], []).append(str(i["indexdef"]))
    return out


def runtime_tables(db: dict[str, Any]) -> set[str]:
    return {t["relname"] for t in db.get("tables") or []}


async def _code(
    sink: EvidenceSink,
    code: CodeIndex,
    path: str,
    lines: list[int],
    summary: str,
    signal: str,
    extra: dict[str, Any] | None = None,
) -> Evidence:
    start, end = lines[0], lines[1]
    excerpt = code.read(
        path, max(1, start - 3), min(code.line_count(path), end + 3) if end - start < 60 else start + 60
    )
    return await sink.add_evidence(
        "code_location",
        {"path": path, "lines": [start, end], "commit": code.commit, "signal": signal, **(extra or {})},
        summary,
        {"excerpt": excerpt[:6000], "signal": signal, **(extra or {})},
    )
