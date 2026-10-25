"""Rule-based hypotheses from the evidence pack (ADR-0015: detectors feed evidence and
hypotheses; the model adds mechanism reasoning when available).

Every hypothesis cites evidence ids with claims built from the evidence itself, so they pass
the same citation checks as model-written hypotheses. Rules never set confidence — the rubric
does.
"""

from __future__ import annotations

from typing import Any

from reliability.agents.diagnosis.citations import Evidence
from reliability.agents.diagnosis.evidence import Pack


def _code(pack: Pack, signal: str) -> list[Evidence]:
    return [e for e in pack.items if e.kind == "code_location" and e.locator.get("signal") == signal]


def _loc(e: Evidence) -> dict[str, Any]:
    return {"path": e.locator["path"], "lines": e.locator["lines"]}


def _metric(pack: Pack) -> Evidence | None:
    if pack.endpoint:
        return pack.first("metric_window", scope=f"endpoint:{pack.endpoint}")
    return next(iter(pack.by_kind("metric_window")), None)


def _metric_claim(e: Evidence) -> str:
    snap = e.snapshot or {}
    p95, err = snap.get("p95_ms"), snap.get("error_rate")
    parts = []
    if p95 is not None:
        parts.append(f"p95 {float(p95):.0f} ms")
    if err:
        parts.append(f"error rate {float(err) * 100:.1f}%")
    return f"{e.locator.get('scope', 'endpoint').split(':', 1)[-1]} has " + " and ".join(parts or ["degraded results"])


def _hyp(
    category: str,
    statement: str,
    mechanism: str,
    fix: str,
    supports: list[tuple[Evidence, str]],
    locations: list[dict[str, Any]],
    experiment: str | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "statement": statement,
        "mechanism": mechanism,
        "fix_direction": fix,
        "code_locations": locations,
        "supporting_evidence": [{"evidence_id": e.id, "claim": c} for e, c in supports if e is not None],
        "contradicting_evidence": [],
        "predicted_experiment": experiment,
        "origin": "rules",
    }


def _status(e: Evidence) -> int | None:
    """HTTP status of a sample (k6 tags carry it as a string)."""
    try:
        return int((e.snapshot or {}).get("status"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def generate(pack: Pack) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    metric = _metric(pack)
    metric_support = [(metric, _metric_claim(metric))] if metric else []
    endpoint = pack.endpoint or "the affected endpoint"
    tables: dict[str, dict[str, Any]] = pack.facts.get("tables") or {}

    def table_ev(name: str) -> Evidence | None:
        return pack.first("db_stat", stat="table", table=name)

    # Missing index: an unindexed filter on a table that is read by full sequential scans.
    for e in _code(pack, "unindexed_filter"):
        table, column = e.locator["table"], e.locator["column"]
        t = tables.get(table)
        supports: list[tuple[Evidence, str]] = [
            (e, f"{e.locator['path']}:{e.locator['lines'][0]} filters {table} by {column} without an index"),
            *metric_support,
        ]
        if not t or int(t.get("seq_scan") or 0) == 0:
            continue  # without scan statistics an unindexed filter is not evidence of a slowdown
        if t:
            tev = table_ev(table)
            if tev:
                supports.append(
                    (
                        tev,
                        f"{table} had {t['seq_scan']} sequential scans reading {t['seq_tup_read']} rows "
                        f"with {t.get('n_live_tup')} live rows",
                    )
                )
            full_scans = t.get("rows_read_per_seq_scan", 0) >= 0.5 * max(1, int(t.get("n_live_tup") or 0))
            if not full_scans:
                continue
        out.append(
            _hyp(
                "missing_index",
                f"{endpoint} scans the whole {table} table because {table}.{column} has no index.",
                f"The handler filters {table} on {column}; without an index Postgres reads every row for each request, "
                "so latency grows with table size and concurrent requests compete for I/O and CPU.",
                f"Add an index on {table}({column}) in a migration and the ORM model.",
                supports,
                [_loc(e)],
                experiment="latency stays high at 1 VU (cost per request, not contention)",
            )
        )

    # N+1: a query per item inside a loop in the hot handler.
    loops = _code(pack, "db_call_in_loop")
    if loops:
        e = loops[0]
        supports = [
            (e, f"{e.locator['path']}:{e.locator['lines'][0]} runs a database query inside a loop"),
            *metric_support,
        ]
        hot = max(tables.values(), key=lambda t: t.get("accesses_per_request") or 0, default=None)
        if hot and (hot.get("accesses_per_request") or 0) >= 3:
            tev = table_ev(hot["relname"])
            if tev:
                supports.append((tev, f"{hot['relname']} was accessed {hot['accesses_per_request']} times per request"))
        out.append(
            _hyp(
                "n_plus_one",
                f"{endpoint} issues one database query per item (N+1 queries).",
                "The handler loads a list and then queries the database again for every element, so the number of "
                "round trips — and the latency — grows with the number of items returned.",
                "Load the related data in one query (join, IN (...) batch, or eager loading) instead of inside "
                "the loop.",
                supports,
                [_loc(x) for x in loops[:3]],
                experiment="latency stays high at 1 VU",
            )
        )

    # Pool exhaustion: a tiny pool plus errors/latency under concurrency.
    pools = _code(pack, "pool_setting")
    conns = pack.facts.get("connections") or {}
    errors = [
        e
        for e in pack.by_kind("http_sample")
        if _status(e) in (500, 502, 503, 504, 0) or "timeout" in e.summary.lower()
    ]
    metric_errors = bool(metric and float((metric.snapshot or {}).get("error_rate") or 0) > 0)
    # Saturated: busy connections (active or idle in a transaction) sit at the configured pool size
    # for most samples. A peak above it means that setting is not the effective limit.
    saturated = bool(
        pools
        and int(pools[0].locator["value"]) <= int(conns.get("peak_busy") or 0) <= int(pools[0].locator["value"]) + 1
        and float(conns.get("plateau_fraction") or 0) >= 0.5
    )
    if pools and (errors or metric_errors or saturated):
        e = pools[0]
        supports = [
            (e, f"{e.locator['path']}:{e.locator['lines'][0]} limits the pool to {e.locator['value']} connections"),
            *metric_support,
        ]
        cev = pack.first("db_stat", stat="connections")
        if cev and conns.get("peak_busy") is not None:
            supports.append((cev, f"busy connections peaked at {conns['peak_busy']}"))
        supports += [(x, f"{x.locator.get('endpoint')} failed with status {_status(x)}") for x in errors[:2]]
        out.append(
            _hyp(
                "pool_exhaustion",
                f"{endpoint} runs out of database connections under load (pool of {e.locator['value']}).",
                "Requests hold a pooled connection for the whole handler; with a very small pool, concurrent requests "
                "wait for a free connection until the pool timeout and then fail.",
                "Size the pool for the expected concurrency and release connections before slow work.",
                supports,
                [_loc(e)] + [_loc(x) for x in _code(pack, "blocking_call")[:1]],
                experiment="errors and latency disappear at 1 VU",
            )
        )

    # Race condition: invariant violation + unlocked read-modify-write.
    if pack.finding.get("type") == "invariant_violation":
        racy = _code(pack, "unlocked_write")
        probe = next((x for x in pack.by_kind("experiment")), None)
        supports = []
        if probe:
            supports.append((probe, f"the concurrency probe violated the invariant ({probe.summary[:120]})"))
        supports += [
            (x, f"{x.locator['path']}:{x.locator['lines'][0]} writes back a value read earlier without a lock")
            for x in racy[:2]
        ]
        if supports:
            out.append(
                _hyp(
                    "race_condition",
                    f"Concurrent requests to {endpoint} interleave a read and a write (check-then-act race).",
                    "The handler reads the current value, decides, then writes; two requests can both pass the check "
                    "before either writes, violating the invariant.",
                    "Make the check and the update atomic: a conditional UPDATE … WHERE, SELECT … FOR UPDATE, or a "
                    "serializable transaction.",
                    supports,
                    [_loc(x) for x in racy[:2]],
                    experiment="passes at 1 VU, fails under concurrency",
                )
            )

    # Blocking I/O: sleep / synchronous I/O in the handler.
    blocking = _code(pack, "blocking_call")
    if blocking and metric:
        e = blocking[0]
        out.append(
            _hyp(
                "blocking_io",
                f"{endpoint} blocks on a synchronous call (sleep or blocking I/O) inside the request handler.",
                "Each request waits for the blocking call; worker threads/event loop are tied up, so latency rises and "
                "throughput drops as load grows.",
                "Remove the blocking call or make it asynchronous / move it off the request path.",
                [(e, f"{e.locator['path']}:{e.locator['lines'][0]} makes a blocking call"), *metric_support],
                [_loc(e)],
                experiment="latency rises with load",
            )
        )

    # Unhandled exceptions: 5xx samples + generic raise in the handler.
    raises = _code(pack, "generic_raise")
    if raises and errors:
        e = raises[0]
        text = " ".join(x.summary.lower() for x in errors) + " " + (e.snapshot or {}).get("excerpt", "").lower()
        category = (
            "external_dependency"
            if any(w in text for w in ("gateway", "upstream", "timeout", "api", "payment", "connection"))
            else "logic_error"
        )
        out.append(
            _hyp(
                category,
                f"{endpoint} fails with HTTP 500 when an unhandled exception is raised in the handler.",
                "An exception from the handler (or a dependency it calls) is not caught or mapped to a client "
                "error, so the framework returns 500 for those requests.",
                "Handle the dependency failure explicitly (timeout, retry with backoff, or a 502/503 response).",
                [
                    (e, f"{e.locator['path']}:{e.locator['lines'][0]} raises an unhandled exception"),
                    *[(x, f"{x.locator.get('endpoint')} failed with status {_status(x)}") for x in errors[:2]],
                    *metric_support,
                ],
                [_loc(e)],
            )
        )
    return out
