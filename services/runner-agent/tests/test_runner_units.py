from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from runner_agent.config import Config
from runner_agent.drivers.base import CommandSpec, Egress, Limits
from runner_agent.drivers.docker import acl_yaml, network_name, proxy_args, run_args
from runner_agent.executor import _safe_extract, _Tail
from runner_agent.k6stream import K6Aggregator, hist_index, hist_value, percentile


def point(metric: str, t: str, value: float, **tags: str) -> str:
    return json.dumps({"type": "Point", "metric": metric, "data": {"time": t, "value": value, "tags": tags}})


def test_histogram_percentiles_are_close() -> None:
    hist: dict[int, int] = {}
    for ms in range(1, 1001):
        i = hist_index(float(ms))
        hist[i] = hist.get(i, 0) + 1
    assert abs(percentile(hist, 0.95) - 950) / 950 < 0.05
    assert abs(hist_value(hist_index(123.0)) - 123) / 123 < 0.05


def test_aggregator_progress_buckets_samples_and_vu_minutes() -> None:
    agg = K6Aggregator(seed=1)
    for i in range(100):
        t = f"2026-10-23T10:00:{i % 10:02d}.{i:03d}123456Z"
        tags = {"endpoint": "GET /a", "trace_id": f"{i:032x}", "status": "500" if i % 10 == 0 else "200"}
        agg.feed_line(point("http_req_duration", t, 10.0 + i, **tags))
        agg.feed_line(point("http_req_failed", t, 1 if i % 10 == 0 else 0, **tags))
        agg.feed_line(point("vus", t, 5))
    agg.feed_line("not json")
    progress = agg.progress(window_s=60)
    assert progress["error_rate"] == pytest.approx(0.1)
    ts = agg.timeseries()
    assert set(ts["scopes"]) == {"overall", "endpoint:GET /a"}
    assert len(ts["samples"]["errors"]) == 10 and ts["samples"]["slowest"][0]["duration_ms"] == 109.0
    assert agg.vu_minutes() == 1


def test_abort_watchdog_requires_sustained_error_rate() -> None:
    agg = K6Aggregator(abort_error_rate=0.5, abort_window_s=30)
    for s in range(40):
        for k in range(5):
            t = f"2026-10-23T10:00:{s:02d}.{k:06d}Z"
            agg.feed_line(point("http_req_duration", t, 5, endpoint="GET /x", trace_id=f"{s}-{k}"))
            agg.feed_line(point("http_req_failed", t, 1, endpoint="GET /x", trace_id=f"{s}-{k}"))
    assert agg.abort_reason and "above 50%" in agg.abort_reason


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        _safe_extract(buf.getvalue(), tmp_path / "out")


def test_tail_reads_only_complete_lines(tmp_path: Path) -> None:
    f = tmp_path / "raw.json"
    f.write_text('{"a":1}\n{"b":')
    tail = _Tail(f)
    assert tail.read_lines() == ['{"a":1}']
    with f.open("a") as fh:
        fh.write("2}\n")
    assert tail.read_lines() == ['{"b":2}']


def test_docker_args_enforce_isolation(tmp_path: Path) -> None:
    cfg = Config(env="production", driver="docker", runner_token="x", workdir=str(tmp_path))
    spec = CommandSpec(
        "k6",
        ["run", "scenarios.js"],
        tmp_path,
        {"BASE_URL": "https://s.example.com"},
        {"PASSWORD": "p"},
        Limits(4, 8, 4096),
    )
    args = run_args(cfg, "abcdef0123456789", spec, tmp_path / ".secrets.env")
    for flag in (
        "--runtime",
        "runsc",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        "4096",
        "no-new-privileges",
        network_name("abcdef0123456789"),
    ):
        assert flag in args
    assert "PASSWORD=p" not in " ".join(args)  # secrets only via --env-file
    assert "--env-file" in args
    proxy = proxy_args(cfg, "abcdef0123456789", tmp_path / "acl.yaml")
    assert "--egress-acl-file" in proxy
    acl = acl_yaml(Egress(["s.example.com"], []))
    assert "action: enforce" in acl and '"s.example.com"' in acl


def test_process_driver_refused_in_production() -> None:
    with pytest.raises(ValueError, match="RA_DRIVER must be docker"):
        Config(env="production", driver="process")
