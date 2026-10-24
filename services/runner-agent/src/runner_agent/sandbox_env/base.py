from __future__ import annotations

import io
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

DB_STATS_SQL = """
SELECT json_build_object(
  'database', (SELECT row_to_json(d) FROM (
      SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit, tup_returned, tup_fetched,
             tup_inserted, tup_updated, tup_deleted, deadlocks, temp_bytes
      FROM pg_stat_database WHERE datname = current_database()) d),
  'tables', (SELECT coalesce(json_agg(t ORDER BY t.seq_tup_read DESC), '[]'::json) FROM (
      SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup, n_tup_ins, n_tup_upd, n_tup_del
      FROM pg_stat_user_tables) t),
  'indexes', (SELECT coalesce(json_agg(i ORDER BY i.idx_scan), '[]'::json) FROM (
      SELECT relname, indexrelname, idx_scan FROM pg_stat_user_indexes) i),
  'connections', (SELECT json_build_object(
      'total', count(*), 'active', count(*) FILTER (WHERE state = 'active'),
      'idle_in_transaction', count(*) FILTER (WHERE state LIKE 'idle in transaction%'),
      'waiting', count(*) FILTER (WHERE wait_event_type = 'Lock'),
      'max', current_setting('max_connections')::int)
      FROM pg_stat_activity WHERE datname = current_database())
)
"""
STATEMENTS_SQL = """
SELECT coalesce(json_agg(s), '[]'::json) FROM (
  SELECT left(query, 500) AS query, calls, round(total_exec_time::numeric, 2) AS total_ms,
         round(mean_exec_time::numeric, 3) AS mean_ms, rows
  FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  ORDER BY total_exec_time DESC LIMIT 15) s
"""
CONNECTIONS_SQL = """
SELECT json_build_object('active', count(*) FILTER (WHERE state = 'active'),
  'idle_in_transaction', count(*) FILTER (WHERE state LIKE 'idle in transaction%'), 'total', count(*))
FROM pg_stat_activity WHERE datname = current_database()
"""


class SandboxError(Exception):
    """The sandbox could not be built or started; ``phase`` says where."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(f"{phase}: {message}")
        self.phase, self.message = phase, message


@dataclass
class SandboxReport:
    strategy: str
    driver: str
    phases: dict[str, float] = field(default_factory=dict)  # phase -> seconds
    healthy: bool = False
    base_url: str | None = None
    services: list[str] = field(default_factory=list)
    migrations: str = "none"  # none | applied | failed
    seed: str = "none"
    instrumentation: str = "unavailable"
    stub_calls: list[dict[str, Any]] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    db: dict[str, Any] | None = None
    db_samples: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_phase: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Phase:
    """Times one phase into the report: ``with Phase(report, "build"): ...``."""

    def __init__(self, report: SandboxReport, name: str) -> None:
        self.report, self.name = report, name

    def __enter__(self) -> None:
        self.started = time.monotonic()

    def __exit__(self, *exc: object) -> None:
        self.report.phases[self.name] = round(time.monotonic() - self.started, 2)


class Sandbox(Protocol):
    report: SandboxReport

    async def build(self) -> None: ...
    async def start(self) -> str: ...  # returns the base URL the generator should use
    async def sample(self) -> None: ...
    async def collect(self) -> None: ...
    async def stop(self) -> None: ...


def fill(value: str, port: int) -> str:
    return value.replace("{port}", str(port))


def extract_source(data: bytes, dest: Path) -> Path:
    """Extract a .tar.gz or .zip source archive safely; strips a single top-level directory
    (GitHub tarballs). Returns the source root."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def target(name: str) -> Path | None:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts:
            return None
        t = (dest / name).resolve()
        return t if root in t.parents else None

    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                t = target(info.filename)
                if t is None or info.is_dir():
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(zf.read(info))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            members = [m for m in tf.getmembers() if (m.isfile() or m.isdir()) and target(m.name) is not None]
            tf.extractall(dest, members=members, filter="data")
    entries = [p for p in dest.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest
