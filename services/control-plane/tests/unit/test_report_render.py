"""Report rendering snapshot (FR-28): a fixed report document renders to the committed HTML.
Regenerate after an intended change with ``UPDATE_SNAPSHOTS=1 pytest tests/unit/test_report_render.py``."""

from __future__ import annotations

import os
import re
from pathlib import Path

from reliability.modules.reports.export import render_html

SNAPSHOT = Path(__file__).parent / "snapshots" / "report_v1.html"
DOC = {
    "schema": "report@1",
    "project": {"id": "p1", "name": "Shop & <Co>"},
    "period": {"from": "2026-10-01T00:00:00+00:00", "to": "2026-10-31T00:00:00+00:00"},
    "generated_at": "2026-10-31T00:00:00+00:00",
    "sections": ["runs", "trends", "coverage", "findings", "repairs"],
    "summary": {
        "runs": {"load": {"failed": 1, "passed": 2}, "functional": {"passed": 4}},
        "open_findings": {"high": 1},
        "capacity": {"vus": 120, "reason": "p95 380 ms crossed 300 ms"},
    },
    "runs": [
        {
            "id": "r3",
            "kind": "load",
            "status": "passed",
            "environment": "sandbox_build",
            "created_at": "2026-10-20T10:00:00+00:00",
            "finished_at": None,
            "p95_ms": 42.5,
            "error_rate": 0.001,
            "rps": 88.1,
        },
        {
            "id": "r2",
            "kind": "load",
            "status": "failed",
            "environment": "target_url",
            "created_at": "2026-10-10T10:00:00+00:00",
            "finished_at": None,
            "p95_ms": 310.0,
            "error_rate": 0.02,
            "rps": 51.0,
        },
    ],
    "trends": {
        "overall": [
            {"run_id": "r2", "at": "a", "p95_ms": 310.0, "error_rate": 0.02},
            {"run_id": "r3", "at": "b", "p95_ms": 42.5, "error_rate": 0.001},
        ],
        "endpoint:GET /api/tasks": [{"run_id": "r3", "at": "b", "p95_ms": 61.0, "error_rate": 0.0}],
    },
    "coverage": {
        "totals": {"discovered": 12, "approved": 9, "tested": 8, "passing": 7},
        "by_source": {
            "code": {"discovered": 10, "approved": 8, "tested": 7, "passing": 6},
            "openapi": {"discovered": 2, "approved": 1, "tested": 1, "passing": 1},
        },
        "by_criticality": {},
    },
    "findings": [
        {
            "id": "f1",
            "title": "<img src=x onerror=alert(1)> GET /api/tasks p95 310 ms",
            "type": "slo_breach",
            "severity": "high",
            "status": "fix_proposed",
            "occurrences": 3,
            "first_seen": "2026-10-10T10:00:00+00:00",
            "last_seen": "2026-10-20T10:00:00+00:00",
        }
    ],
    "repairs": [
        {
            "id": "a1",
            "finding_id": "f1",
            "attempt_no": 1,
            "status": "pr_opened",
            "diff_stats": {"files": 2, "additions": 30, "deletions": 1},
            "gates": {"build": "passed", "fail_to_pass": "passed", "load_comparison": "passed"},
            "comparisons": [
                {
                    "scope": "endpoint:GET /api/tasks",
                    "baseline_p95_ms": 85.1,
                    "candidate_p95_ms": 4.2,
                    "delta_pct": -95.1,
                    "ci_pct": [-95.6, -94.3],
                    "verdict": "improved",
                }
            ],
            "pull_request": 'https://github.com/acme/shop/pull/7"><b>',
        }
    ],
}


def test_report_html_matches_snapshot() -> None:
    page = render_html(DOC)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.write_text(page)
    assert page == SNAPSHOT.read_text()


def test_report_html_is_self_contained_and_escaped() -> None:
    page = render_html(DOC)
    assert "<script" not in page.lower()
    assert 'href="https://github.com/acme/shop/pull/7&quot;&gt;&lt;b&gt;"' in page  # attribute breakout escaped
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "Shop &amp; &lt;Co&gt;" in page
    assert not re.search(r"""src=["']https?://""", page)  # nothing is loaded from elsewhere
    assert "−95.1" in page or "-95.1%" in page
    assert page.count("<polyline") == 1  # only series with ≥ 2 points get a sparkline
