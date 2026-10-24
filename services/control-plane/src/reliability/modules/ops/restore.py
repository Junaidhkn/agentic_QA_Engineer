"""Restore drills (operations §7, NFR-9): prove that backups restore and the restored data is
sound, and measure how long it takes (RTO evidence).

Two ways to get a restored copy:

* ``rds`` (staging/production, weekly ECS scheduled task): restore the latest automated RDS
  snapshot of the production instance into a temporary instance in the staging network, run
  the checks, delete the instance.
* ``dump`` (local, CI and ad-hoc): ``pg_dump`` the source, ``pg_restore`` into a scratch
  database on any server, run the checks, drop it.

Checks on the restored copy: Alembic revision equals the source's, row counts of key tables
match the source (within ``tolerance`` for a live source), and every integrity invariant holds,
including audit-chain verification (``integrity.run_checks``). The outcome is recorded as the
``restore_drill`` platform check.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from reliability.modules.ops import checks, integrity

KEY_TABLES = (
    "organizations",
    "users",
    "projects",
    "targets",
    "workflows",
    "runs",
    "findings",
    "hypotheses",
    "repair_attempts",
    "artifacts",
    "audit_events",
)


@dataclass
class DrillResult:
    ok: bool
    mode: str
    started_at: str
    timings_s: dict[str, float] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    restored_counts: dict[str, int] = field(default_factory=dict)
    count_mismatches: dict[str, list[int]] = field(default_factory=dict)
    revision: dict[str, str | None] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, [])}


def libpq(url: str) -> str:
    """``postgresql+psycopg://…`` → ``postgresql://…`` for the command-line tools."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql"))


def sqlalchemy_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql+psycopg"))


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


async def _snapshot(url: str) -> tuple[dict[str, int], str | None]:
    engine = create_async_engine(sqlalchemy_url(url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            # KEY_TABLES are constants.
            counts = {t: int(await conn.scalar(text(f"SELECT count(*) FROM {t}")) or 0) for t in KEY_TABLES}  # noqa: S608
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        return counts, revision
    finally:
        await engine.dispose()


async def _checks_on(url: str) -> dict[str, Any]:
    engine = create_async_engine(sqlalchemy_url(url), poolclass=NullPool)
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            return await integrity.run_checks(session)
    finally:
        await engine.dispose()


def _compare(source: dict[str, int], restored: dict[str, int], tolerance: float) -> dict[str, list[int]]:
    """Restored counts may trail a live source by ``tolerance`` (fraction), never exceed it."""
    out = {}
    for table, n in source.items():
        got = restored.get(table, -1)
        if got > n or got < n - max(int(n * tolerance), 0):
            out[table] = [n, got]
    return out


async def verify_copy(result: DrillResult, source_url: str, restored_url: str, tolerance: float) -> None:
    t = time.monotonic()
    result.restored_counts, restored_rev = await _snapshot(restored_url)
    result.revision["restored"] = restored_rev
    result.count_mismatches = _compare(result.source_counts, result.restored_counts, tolerance)
    result.integrity = await _checks_on(restored_url)
    result.timings_s["checks"] = round(time.monotonic() - t, 2)
    result.ok = (
        not result.count_mismatches
        and result.revision.get("source") == restored_rev
        and bool(result.integrity.get("ok"))
    )
    if not result.ok and result.error is None:
        problems = []
        if result.count_mismatches:
            problems.append(f"row counts differ: {sorted(result.count_mismatches)}")
        if result.revision.get("source") != restored_rev:
            problems.append("migration revision differs")
        if not result.integrity.get("ok"):
            problems.append("integrity invariants failed")
        result.error = "; ".join(problems)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    # Fixed pg_dump/pg_restore/psql argv built from operator-supplied URLs; no shell.
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=6 * 3600
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-500:]}")


async def dump_drill(
    source_url: str, scratch_server_url: str, *, tolerance: float = 0.0, keep: bool = False
) -> DrillResult:
    """pg_dump → pg_restore into a fresh scratch database → checks → drop."""
    result = DrillResult(ok=False, mode="dump", started_at=datetime.now(UTC).isoformat())
    scratch_db = f"re_restore_drill_{uuid.uuid4().hex[:10]}"
    admin = libpq(scratch_server_url)
    restored = with_database(admin, scratch_db)
    created = False
    with tempfile.TemporaryDirectory(prefix="re-restore-") as tmp:
        dump = os.path.join(tmp, "db.dump")
        try:
            result.source_counts, result.revision["source"] = await _snapshot(source_url)
            t = time.monotonic()
            _run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={dump}", libpq(source_url)])
            result.timings_s["dump"] = round(time.monotonic() - t, 2)
            t = time.monotonic()
            _run(["psql", admin, "-v", "ON_ERROR_STOP=1", "-qc", f'CREATE DATABASE "{scratch_db}"'])
            created = True
            _run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", f"--dbname={restored}", dump])
            result.timings_s["restore"] = round(time.monotonic() - t, 2)
            await verify_copy(result, source_url, restored, tolerance)
        except Exception as exc:  # recorded as a failed drill, never raised past the caller
            result.ok, result.error = False, f"{type(exc).__name__}: {exc}"[:1000]
        finally:
            if created and not keep:
                _run(["psql", admin, "-qc", f'DROP DATABASE IF EXISTS "{scratch_db}" WITH (FORCE)'])
    result.timings_s["total"] = round(sum(result.timings_s.values()), 2)
    return result


@dataclass(frozen=True)
class RdsDrillConfig:
    source_instance: str
    drill_instance_prefix: str
    subnet_group: str
    security_group_ids: list[str]
    instance_class: str
    user: str
    password: str
    database: str = "reliability"
    sslmode: str = "require"


async def rds_drill(
    cfg: RdsDrillConfig,
    *,
    rds: Any,
    tolerance: float = 0.001,
    source_url: str,
    wait: Callable[[str], None] | None = None,
) -> DrillResult:
    """Restore the newest automated snapshot of ``source_instance`` into a temporary instance."""
    result = DrillResult(ok=False, mode="rds", started_at=datetime.now(UTC).isoformat())
    drill_id = f"{cfg.drill_instance_prefix}-{datetime.now(UTC):%Y%m%d%H%M}"
    created = False
    try:
        snaps = rds.describe_db_snapshots(DBInstanceIdentifier=cfg.source_instance, SnapshotType="automated")[
            "DBSnapshots"
        ]
        snaps = [s for s in snaps if s.get("Status") == "available"]
        if not snaps:
            raise RuntimeError(f"no available automated snapshot of {cfg.source_instance}")
        latest = max(snaps, key=lambda s: s["SnapshotCreateTime"])
        result.integrity["snapshot"] = latest["DBSnapshotIdentifier"]
        age_h = (datetime.now(UTC) - latest["SnapshotCreateTime"]).total_seconds() / 3600
        result.timings_s["snapshot_age_h"] = round(age_h, 2)
        # Counts at snapshot time are unknown for a live source; compare against now with a tolerance
        # and only flag restored > source or a large shortfall.
        result.source_counts, result.revision["source"] = await _snapshot(source_url)
        t = time.monotonic()
        rds.restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=drill_id,
            DBSnapshotIdentifier=latest["DBSnapshotIdentifier"],
            DBInstanceClass=cfg.instance_class,
            DBSubnetGroupName=cfg.subnet_group,
            VpcSecurityGroupIds=cfg.security_group_ids,
            MultiAZ=False,
            PubliclyAccessible=False,
            DeletionProtection=False,
            Tags=[{"Key": "Purpose", "Value": "restore-drill"}],
        )
        created = True
        (wait or (lambda ident: rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=ident)))(drill_id)
        result.timings_s["restore"] = round(time.monotonic() - t, 2)
        endpoint = rds.describe_db_instances(DBInstanceIdentifier=drill_id)["DBInstances"][0]["Endpoint"]
        restored = (
            f"postgresql://{cfg.user}:{cfg.password}@{endpoint['Address']}:{endpoint['Port']}/{cfg.database}"
            f"?sslmode={cfg.sslmode}"
        )
        await verify_copy(result, source_url, restored, tolerance=max(tolerance, age_h * 0.01))
    except Exception as exc:
        result.ok, result.error = False, f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        if created:
            rds.delete_db_instance(DBInstanceIdentifier=drill_id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
    result.timings_s["total"] = round(sum(v for k, v in result.timings_s.items() if k != "snapshot_age_h"), 2)
    return result


async def record(result: DrillResult) -> None:
    from reliability.platform.db import system_tx

    async with system_tx() as s:
        await checks.record(s, checks.RESTORE_DRILL, result.ok, result.as_detail())
