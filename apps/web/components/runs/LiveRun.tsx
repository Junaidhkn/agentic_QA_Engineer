"use client";

import { useEffect, useState } from "react";

import { RunCharts } from "@/components/runs/RunCharts";
import { Card } from "@/components/ui";
import { currentToken } from "@/lib/api";
import { subscribeRunEvents } from "@/lib/sse";
import type { SeriesPoint } from "@/lib/types";

interface Progress { t: number; vus: number; rps: number; error_rate: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null; generator_cpu?: number }

/** Live progress over SSE (FR-15, ADR-0021); ≤ 5 s behind the generator. */
export function LiveRun({ runId, onStatus, p95Threshold }: { runId: string; onStatus?: (status: string) => void; p95Threshold?: number }) {
  const [points, setPoints] = useState<SeriesPoint[]>([]);
  const [last, setLast] = useState<Progress | null>(null);
  useEffect(() => subscribeRunEvents(runId, currentToken, (msg) => {
    if (msg.event === "progress") {
      const p = msg.data as Progress;
      setLast(p);
      setPoints((prev) => [...prev.filter((x) => x.t !== p.t), { t: p.t, vus: p.vus, rps: p.rps, error_rate: p.error_rate, p50_ms: p.p50_ms, p95_ms: p.p95_ms, p99_ms: p.p99_ms }].slice(-720));
    } else if (msg.event === "status") {
      onStatus?.(String((msg.data as { status: string }).status));
    }
  }, () => onStatus?.("ended")), [runId, onStatus]);

  return (
    <div className="space-y-4">
      <Card title="Live">
        {!last ? <p className="text-sm text-[var(--muted)]">Waiting for the first measurements…</p> : (
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-6" aria-live="polite">
            {[["Virtual users", last.vus], ["Requests/s", last.rps], ["Failed", `${(last.error_rate * 100).toFixed(2)}%`], ["p50", `${last.p50_ms ?? "—"} ms`], ["p95", `${last.p95_ms ?? "—"} ms`], ["Generator CPU", last.generator_cpu !== undefined ? `${Math.round(last.generator_cpu * 100)}%` : "—"]].map(([k, v]) => (
              <div key={String(k)}><dt className="text-xs text-[var(--muted)]">{k}</dt><dd className="text-lg font-semibold">{String(v)}</dd></div>
            ))}
          </dl>
        )}
      </Card>
      {points.length > 1 && <RunCharts points={points} p95Threshold={p95Threshold} />}
    </div>
  );
}
