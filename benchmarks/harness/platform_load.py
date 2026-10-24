"""Platform load test (NFR-4, evaluation.md §5 "95% jobs start < 30 s").

Replays the reference platform load against a deployed control plane through the public API:
``--load-concurrency`` load runs kept active at all times (default 25) plus functional runs
arriving at ``--functional-per-hour`` (default 100), for ``--minutes``. Every run's job start
is ``started_at − queued_at`` as recorded by the platform (queued → first container running).

Each *lane* is one verified target with a ready suite and a small load profile; a target
accepts one active load run at a time, so load concurrency = number of lanes. Lanes come
from a JSON file (one entry per target; API keys need ``runs:read`` + ``runs:write`` and the
targets must already have had one approved load run):

    [{"api_key": "re_…", "project_id": "…", "target_id": "…", "load_profile_id": "…"}, …]

Exit code 0 when p95 job start < 30 s and no run errored for platform reasons; 1 otherwise.
The report (JSON + Markdown) goes to ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"passed", "failed", "invalid", "cancelled", "errored", "rejected"}
SLO_P95_S = 30.0
# Errors that say the platform (not the target) failed the run.
PLATFORM_ERRORS = ("runner lost", "lease", "internal", "execution paused")


@dataclass(frozen=True)
class Lane:
    api_key: str
    project_id: str
    target_id: str
    load_profile_id: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass
class Tracker:
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def _submit(client: httpx.AsyncClient, lane: Lane, kind: str, tracker: Tracker) -> str | None:
    body: dict[str, Any] = {"kind": kind, "environment": {"type": "target_url", "target_id": lane.target_id}}
    if kind == "load":
        body["load_profile_id"] = lane.load_profile_id
    r = await client.post(
        f"/v1/projects/{lane.project_id}/runs",
        json=body,
        headers={**lane.headers(), "Idempotency-Key": f"platform-load-{uuid.uuid4()}"},
    )
    if r.status_code != 202:
        tracker.rejected.append({"kind": kind, "status": r.status_code, "body": r.text[:300]})
        return None
    run = r.json()
    tracker.runs[run["id"]] = {"lane": lane.target_id, **run}
    if run["status"] == "pending_approval":
        tracker.rejected.append({"kind": kind, "status": 202, "body": "run needs approval; warm up the lane first"})
    return str(run["id"])


async def _wait(
    client: httpx.AsyncClient, lane: Lane, run_id: str, tracker: Tracker, poll_s: float, give_up_at: float
) -> dict[str, Any]:
    """Poll until the run ends; a run that disappears or outlives ``give_up_at`` is a platform failure."""
    missing = 0
    while True:
        r = await client.get(f"/v1/runs/{run_id}", headers=lane.headers())
        if r.status_code == 200:
            missing = 0
            tracker.runs[run_id] = {"lane": lane.target_id, **r.json()}
            if r.json()["status"] in TERMINAL or r.json()["status"] == "pending_approval":
                return tracker.runs[run_id]
        elif r.status_code == 404:
            missing += 1
            if missing >= 3:
                tracker.runs[run_id] = {**tracker.runs.get(run_id, {}), "status": "lost"}
                return tracker.runs[run_id]
        if time.monotonic() > give_up_at:
            tracker.runs[run_id] = {**tracker.runs.get(run_id, {}), "status": "timed_out"}
            return tracker.runs[run_id]
        await asyncio.sleep(poll_s)


async def _load_lane(
    client: httpx.AsyncClient, lane: Lane, deadline: float, tracker: Tracker, poll_s: float, give_up_at: float
) -> None:
    while time.monotonic() < deadline:
        run_id = await _submit(client, lane, "load", tracker)
        if run_id is None:
            await asyncio.sleep(poll_s * 2)
            continue
        if (await _wait(client, lane, run_id, tracker, poll_s, give_up_at))["status"] in (
            "pending_approval",
            "lost",
            "timed_out",
        ):
            return


async def _functional_arrivals(
    client: httpx.AsyncClient,
    lanes: list[Lane],
    per_hour: float,
    deadline: float,
    tracker: Tracker,
    poll_s: float,
    rng: random.Random,
    give_up_at: float,
) -> None:
    waits: list[asyncio.Task[Any]] = []
    while per_hour > 0 and time.monotonic() < deadline:
        lane = rng.choice(lanes)
        run_id = await _submit(client, lane, "functional", tracker)
        if run_id is not None:
            waits.append(asyncio.create_task(_wait(client, lane, run_id, tracker, poll_s, give_up_at)))
        await asyncio.sleep(rng.expovariate(per_hour / 3600.0))  # Poisson arrivals
    await asyncio.gather(*waits)


def _seconds(run: dict[str, Any]) -> float | None:
    if not run.get("queued_at") or not run.get("started_at"):
        return None
    return (datetime.fromisoformat(run["started_at"]) - datetime.fromisoformat(run["queued_at"])).total_seconds()


def summarize(tracker: Tracker, minutes: float, lanes: int, per_hour: float) -> dict[str, Any]:
    by_kind: dict[str, list[float]] = {}
    statuses: dict[str, int] = {}
    platform_errors = []
    never_started = 0
    for run in tracker.runs.values():
        statuses[run["status"]] = statuses.get(run["status"], 0) + 1
        s = _seconds(run)
        if s is None:
            if run["status"] in TERMINAL:
                never_started += 1
        else:
            by_kind.setdefault(run["kind"], []).append(s)
        reason = (run.get("status_reason") or "").lower()
        if run["status"] in ("lost", "timed_out"):
            platform_errors.append({"run_id": run.get("id"), "reason": run["status"]})
        elif run["status"] == "errored" and any(p in reason for p in PLATFORM_ERRORS):
            platform_errors.append({"run_id": run["id"], "reason": run.get("status_reason")})
    every = [v for vs in by_kind.values() for v in vs]
    p95 = percentile(every, 0.95)
    return {
        "config": {"minutes": minutes, "load_concurrency": lanes, "functional_per_hour": per_hour},
        "runs": len(tracker.runs),
        "statuses": statuses,
        "job_start_s": {
            "all": {"n": len(every), "p50": percentile(every, 0.5), "p95": p95, "max": max(every, default=None)},
            **{
                k: {"n": len(v), "p50": percentile(v, 0.5), "p95": percentile(v, 0.95), "max": max(v)}
                for k, v in sorted(by_kind.items())
            },
        },
        "never_started": never_started,
        "platform_errors": platform_errors,
        "rejected_submissions": tracker.rejected,
        "slo": {"p95_target_s": SLO_P95_S},
        "passed": p95 is not None and p95 < SLO_P95_S and not platform_errors and not tracker.rejected,
    }


def markdown(report: dict[str, Any]) -> str:
    js = report["job_start_s"]

    def fmt(v: float | None) -> str:
        return "–" if v is None else f"{v:.1f} s"

    lines = [
        "# Platform load test",
        "",
        f"{report['config']['load_concurrency']} concurrent load runs + {report['config']['functional_per_hour']:g} "
        f"functional runs/hour for {report['config']['minutes']:g} min: {report['runs']} runs.",
        "",
        "| Jobs | n | p50 | p95 | max |",
        "|---|---|---|---|---|",
        *[f"| {k} | {v['n']} | {fmt(v['p50'])} | {fmt(v['p95'])} | {fmt(v['max'])} |" for k, v in js.items()],
        "",
        f"Statuses: {', '.join(f'{k} {v}' for k, v in sorted(report['statuses'].items()))}. "
        f"Platform errors: {len(report['platform_errors'])}. "
        f"Rejected submissions: {len(report['rejected_submissions'])}.",
        "",
        f"**{'PASS' if report['passed'] else 'FAIL'}** — p95 job start {fmt(js['all']['p95'])} "
        f"(target < {report['slo']['p95_target_s']:g} s).",
    ]
    return "\n".join(lines) + "\n"


async def run(
    api_url: str,
    lanes: list[Lane],
    *,
    minutes: float,
    functional_per_hour: float,
    poll_s: float = 2.0,
    seed: int = 7,
    settle_minutes: float = 30,
) -> dict[str, Any]:
    tracker = Tracker()
    deadline = time.monotonic() + minutes * 60
    give_up_at = deadline + settle_minutes * 60  # runs still unfinished by then count as failures
    rng = random.Random(seed)
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        await asyncio.gather(
            *(_load_lane(client, lane, deadline, tracker, poll_s, give_up_at) for lane in lanes),
            _functional_arrivals(client, lanes, functional_per_hour, deadline, tracker, poll_s, rng, give_up_at),
        )
    return summarize(tracker, minutes, len(lanes), functional_per_hour)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api-url", required=True)
    p.add_argument("--lanes", required=True, type=Path, help="JSON file with one lane per target")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--functional-per-hour", type=float, default=100)
    p.add_argument("--poll-seconds", type=float, default=5)
    p.add_argument("--settle-minutes", type=float, default=30, help="how long to wait for runs after the window")
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "out")
    args = p.parse_args(argv)
    lanes = [Lane(**entry) for entry in json.loads(args.lanes.read_text())]
    report = asyncio.run(
        run(
            args.api_url,
            lanes,
            minutes=args.minutes,
            functional_per_hour=args.functional_per_hour,
            poll_s=args.poll_seconds,
            settle_minutes=args.settle_minutes,
        )
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "platform-load.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.out / "platform-load.md").write_text(markdown(report))
    print(markdown(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
