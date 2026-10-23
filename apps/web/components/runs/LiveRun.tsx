"use client";

import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/ui";
import { apiFetch } from "@/lib/api";

interface Progress { t: number; vus: number; rps: number; error_rate: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null; generator_cpu?: number }
interface EventRow { seq: number; type: string; payload: Progress & Record<string, unknown> }

/** Live progress (FR-15). Polls the event log; replaced by SSE streaming in the live view. */
export function LiveRun({ runId, active }: { runId: string; active: boolean }) {
  const events = useQuery({ queryKey: ["run-events", runId], queryFn: () => apiFetch<EventRow[]>(`/v1/runs/${runId}/events-log`), refetchInterval: active ? 2000 : 5000 });
  const progress = (events.data ?? []).filter((e) => e.type === "progress").map((e) => e.payload);
  const last = progress.at(-1);
  return (
    <Card title="Live">
      {!last ? <p className="text-sm text-[var(--muted)]">Waiting for the first measurements…</p> : (
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-6">
          {[["VUs", last.vus], ["RPS", last.rps], ["Errors", `${(last.error_rate * 100).toFixed(2)}%`], ["p50", `${last.p50_ms ?? "—"} ms`], ["p95", `${last.p95_ms ?? "—"} ms`], ["Generator CPU", last.generator_cpu !== undefined ? `${Math.round(last.generator_cpu * 100)}%` : "—"]].map(([k, v]) => (
            <div key={String(k)}><dt className="text-xs text-[var(--muted)]">{k}</dt><dd className="text-lg font-semibold">{String(v)}</dd></div>
          ))}
        </dl>
      )}
    </Card>
  );
}
